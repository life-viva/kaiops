import os

from fastapi import FastAPI
from openai import OpenAI
from pydantic import BaseModel

app = FastAPI(title="KaiOps Chat")

client = OpenAI(
    api_key=os.environ["LLM_API_KEY"],
    base_url="https://api.deepseek.com",
)


class ChatRequest(BaseModel):
    message: str


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/chat")
def chat(req: ChatRequest):
    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": req.message}],
    )
    return {"reply": resp.choices[0].message.content}
