import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pymysql
import pymysql.cursors
import qdrant_client
import redis
from fastapi import FastAPI
from openai import OpenAI
from pydantic import BaseModel
from qdrant_client.models import Distance, PointStruct, VectorParams

DB_CONFIG = dict(
    host=os.environ.get("MYSQL_HOST", "mysql"),
    # 【面试考点】不要读名为 MYSQL_PORT/REDIS_PORT 的环境变量！
    # K8s 默认给同命名空间每个 Service 注入 docker-link 风格变量
    # （MYSQL_PORT="tcp://10.x.x.x:3306"），int() 解析直接崩。
    # 端口这种集群内固定值写死即可；真要可配，换个不冲突的变量名。
    port=3306,
    user="root",
    password=os.environ["MYSQL_PASSWORD"],
    database="kaiops",
    autocommit=True,
)

REDIS = redis.Redis(
    host=os.environ.get("REDIS_HOST", "redis"),
    port=6379,
    decode_responses=True,
)

LLM = OpenAI(
    api_key=os.environ["LLM_API_KEY"],
    base_url="https://api.deepseek.com",
)

# embedding 用硅基流动的 bge-m3（OpenAI 兼容接口，免费额度够实验用）
# 【面试考点】为什么 chat 和 embedding 用两家：选型按"任务最优"而不是"供应商统一"，
# DeepSeek 没有 embedding 接口，bge-m3 是中文检索的主流选择
EMBED = OpenAI(
    api_key=os.environ["EMBED_API_KEY"],
    base_url=os.environ.get("EMBED_BASE_URL", "https://api.siliconflow.cn/v1"),
)
EMBED_MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-m3")
EMBED_DIM = 1024  # bge-m3 的向量维度

QDRANT = qdrant_client.QdrantClient(
    host=os.environ.get("QDRANT_HOST", "qdrant"), port=6333, timeout=10
)
KB_COLLECTION = "kb"
KB_DIR = Path(__file__).parent / "knowledge"
CHUNK_SIZE = 600  # 【面试考点】chunk 的取舍：太大→检索粒度粗、噪声多；太小→上下文碎、召回片面
TOP_K = 4

HISTORY_TTL = 3600  # 会话上下文在 Redis 的保留时长（秒）


def db():
    return pymysql.connect(**DB_CONFIG)


def init_db():
    conn = db()
    try:
        with conn.cursor() as cur:
            # 【面试考点】联合索引 (session_id, id)：查询模式是
            # WHERE session_id=? ORDER BY id，最左前缀 + 索引内有序，免 filesort
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_history (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    session_id VARCHAR(64) NOT NULL,
                    role VARCHAR(16) NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_session (session_id, id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
    finally:
        conn.close()


# ---------------- RAG 知识库 ----------------


def embed(texts: list[str]) -> list[list[float]]:
    resp = EMBED.embeddings.create(model=EMBED_MODEL, input=texts)
    return [d.embedding for d in resp.data]


def chunk_text(text: str) -> list[str]:
    """按空行分段，小段合并、大段硬切，目标 ~CHUNK_SIZE 字。"""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        if len(buf) + len(p) < CHUNK_SIZE:
            buf = f"{buf}\n{p}".strip()
        else:
            if buf:
                chunks.append(buf)
            while len(p) > CHUNK_SIZE:  # 单段超长就硬切
                chunks.append(p[:CHUNK_SIZE])
                p = p[CHUNK_SIZE:]
            buf = p
    if buf:
        chunks.append(buf)
    return chunks


def init_kb():
    """建集合 + 把镜像里的 knowledge/*.md 切块、向量化、入库。

    点 ID 用 文件名#序号 的 uuid5 → 确定性 ID，重复启动是幂等覆盖而不是重复堆积。
    """
    if not KB_DIR.exists():
        raise RuntimeError(f"知识库目录不存在: {KB_DIR}")
    if not QDRANT.collection_exists(KB_COLLECTION):
        QDRANT.create_collection(
            collection_name=KB_COLLECTION,
            vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
        )
    for f in sorted(KB_DIR.glob("*.md")):
        chunks = chunk_text(f.read_text(encoding="utf-8"))
        vectors = embed(chunks)
        QDRANT.upsert(
            collection_name=KB_COLLECTION,
            points=[
                PointStruct(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{f.name}#{i}")),
                    vector=v,
                    payload={"source": f.name, "text": c},
                )
                for i, (c, v) in enumerate(zip(chunks, vectors))
            ],
        )
    info = QDRANT.get_collection(KB_COLLECTION)
    print(f"知识库就绪: {info.points_count} 个向量块", flush=True)


def retrieve(question: str):
    vec = embed([question])[0]
    res = QDRANT.query_points(collection_name=KB_COLLECTION, query=vec, limit=TOP_K)
    return res.points


# ---------------- 应用 ----------------


def load_history(session_id: str):
    """多级读：Redis 命中直接用；未命中回源 MySQL 并回填缓存（cache aside 的读路径）。

    返回 (history, cache_hit)，cache_hit 进接口响应——用数据证明缓存真的在工作，
    也是可观测性的习惯：关键行为要能被看见。
    """
    key = f"chat:{session_id}"
    if REDIS.exists(key):
        rows = [json.loads(m) for m in REDIS.lrange(key, 0, -1)]
        return rows, True
    conn = db()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT role, content FROM chat_history "
                "WHERE session_id = %s ORDER BY id",
                (session_id,),
            )
            rows = [{"role": r["role"], "content": r["content"]} for r in cur.fetchall()]
    finally:
        conn.close()
    pipe = REDIS.pipeline()
    pipe.delete(key)
    for r in rows:
        pipe.rpush(key, json.dumps(r))
    pipe.expire(key, HISTORY_TTL)
    pipe.execute()
    return rows, False


def save_message(session_id: str, role: str, content: str):
    """双写：MySQL 是持久层，Redis 是热缓存。

    【面试考点】严格的旁路缓存（cache aside）写路径是"写库后删缓存"；
    这里为了演示直观采用双写追加。双写中途失败会造成短暂不一致，
    靠 TTL 自愈——两种方案的取舍要能在面试里讲清楚。
    """
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_history (session_id, role, content) "
                "VALUES (%s, %s, %s)",
                (session_id, role, content),
            )
    finally:
        conn.close()
    key = f"chat:{session_id}"
    REDIS.rpush(key, json.dumps({"role": role, "content": content}))
    REDIS.expire(key, HISTORY_TTL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # 启动时建表（幂等）。MySQL 不通时 Pod 会启动失败——fail fast
    init_kb()  # 建知识库集合并导入镜像内的文档
    yield


app = FastAPI(title="KaiOps Chat", lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


class AskDocsRequest(BaseModel):
    question: str


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/chat")
def chat(req: ChatRequest):
    history, cache_hit = load_history(req.session_id)
    messages = history + [{"role": "user", "content": req.message}]

    t0 = time.time()
    resp = LLM.chat.completions.create(model="deepseek-chat", messages=messages)
    reply = resp.choices[0].message.content
    elapsed = round(time.time() - t0, 2)

    save_message(req.session_id, "user", req.message)
    save_message(req.session_id, "assistant", reply)
    return {
        "reply": reply,
        "elapsed": elapsed,
        "history_turns": len(history) // 2,
        "cache_hit": cache_hit,
    }


@app.post("/ask_docs")
def ask_docs(req: AskDocsRequest):
    """RAG 问答：检索知识库 top-k → 拼上下文 → LLM 依据资料回答。"""
    hits = retrieve(req.question)
    context = "\n---\n".join(f"[资料{i + 1}] {h.payload['text']}" for i, h in enumerate(hits))
    prompt = (
        "你是运维知识库助手。请只依据下面的资料回答问题；"
        "如果资料不足以回答，直接说明，不要编造。\n\n"
        f"{context}\n\n问题: {req.question}"
    )
    resp = LLM.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
    )
    sources = list(dict.fromkeys(h.payload["source"] for h in hits))
    return {"answer": resp.choices[0].message.content, "sources": sources}
