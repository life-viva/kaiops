# Day 7：给 AI 应用接上记忆——以及一场没能完全定罪的零停机实验

> 承接 [Day 4~5 应用部署](day4-app-deployment.md)。本篇做两件事：**v0.2 应用接入 MySQL 对话历史 + Redis 会话缓存**；然后用高频探针验证滚动更新的零停机——实验结果出乎意料，从"一次失败"开始，最终演变成一场五轮对照、没能完全定罪的生产级排查。全程实录。

## 一、v0.2：记忆的架构

```
POST /chat {message, session_id}
   │
   ├─ load_history(session)   ← Redis 命中直接用；未命中回源 MySQL 并回填（cache aside 读路径）
   ├─ 拼 messages 调 DeepSeek
   └─ save_message ×2         ← 双写：MySQL INSERT + Redis RPUSH（TTL 1h）
```

两个设计决策值得说：

1. **cache_hit 进响应体**。缓存有没有在工作，用数据说话而不是靠"应该生效了"。这也是可观测性的习惯：关键行为要能被看见。
2. **双写 + TTL 自愈，而非严格旁路缓存**。教科书方案是"写库后删缓存"，但那样演示里缓存永远在 miss。我选了双写追加，代价是一致性窗口——这正好成了后面面试题的活教材（写库成功、写缓存前 Pod 挂了 → 该会话读到旧一轮历史，1 小时 TTL 过期后回源自愈）。

表结构一个细节：`INDEX idx_session (session_id, id)`——查询模式是 `WHERE session_id=? ORDER BY id`，联合索引最左前缀 + 索引内有序，免 filesort。

## 二、v0.2 上线的三个坑

**坑 1：`MYSQL_PORT` 环境变量冲突——v0.2 发布即 CrashLoopBackOff**

新 Pod 无限崩溃，日志：`ValueError: invalid literal for int() with base 10: 'tcp://10.111.25.36:3306'`。

根因：K8s 默认（`enableServiceLinks: true`）给同命名空间**每个 Service** 注入 docker-link 风格变量——建了 `mysql` Service，容器里就自动有了 `MYSQL_PORT=tcp://<ClusterIP>:3306`。应用代码 `int(os.environ.get("MYSQL_PORT", "3306"))` 恰好读到它，启动即崩。

修复双保险：代码里端口写死（不读 `*_PORT` 这类高危名字）；pod spec 加 `enableServiceLinks: false`。**教训：应用的环境变量命名空间和 K8s 的注入规则有交集，`{SVC}_PORT`、`{SVC}_SERVICE_HOST` 这些名字不要碰。**

顺带一条铁律：修了代码必须换镜像 tag（v0.2 → v0.2.1）。`IfNotPresent` 下同名 tag 的旧镜像会一直被复用，修复"看起来部署了"但永远不会生效。

**坑 2：scp 覆盖密钥——gitignore 挡得住 git，挡不住 scp**

某次同步代码后所有请求变成 401，DeepSeek 的报错里掩码 key 尾巴是 `****E-ME`——REPLACE-ME，集群里的 Secret 变成了占位符。

时间线：Windows 仓库里存在一个占位符版的 `01-secret.yaml`（真实版只在 master 上）→ `scp -r` 同步目录时把 master 上填过真 key 的文件覆盖 → `kubectl apply` 把占位符推进集群 → 新 Pod 注入假 key。

修复后的格局：**仓库里只有 `.example` 模板，真身只活在 master 的 `/root/secrets/`（同步区之外）和集群对象里**。代码同步路径永远碰不到密钥——这是 GitOps 管理密钥的最基本思路。

**坑 3（小）**：`podman save` 报 `docker-archive doesn't support modifying existing images`——输出文件已存在（上次的 v0.1 包还在 /tmp），删掉或带版本号命名。

## 三、零停机实验：从 1 次失败到一场悬案

实验装置：master 上高频探针循环（0.2s 间隔，`--max-time 3`），另一窗口触发 `rollout restart`，只记录失败。

### 五轮实验矩阵

| 轮 | preStop | 节点负载 | 结果 |
|---|---|---|---|
| A | 无 | 常态 | 1 失败 / 2482 |
| B | 有 | 常态 | 4 失败 |
| C | 有 | 常态 | 0 失败 |
| D | 有 | 每节点压 1 核 | 0 / 524 |
| E | 有 | 每节点压满 2 核 | 0 / 536 |

### 排查过程

**第一反应（错了一半）**：A 轮的失败是经典的 endpoint 传播延迟竞态——Pod 被摘出 endpoints 的瞬间，kube-proxy 的 iptables 规则还没同步完，连接打到垂死 Pod。标准解法 preStop sleep 10（Pod 被杀前多活 10 秒，覆盖同步延迟），加上 `terminationGracePeriodSeconds: 30`。

**修复后不降反升**：B 轮 4 次失败。SRE 的标准动作是先验证修复确实在跑（`kubectl get deploy -o jsonpath` 确认 lifecycle 在线）——在跑。那说明归因不完整。

**独立证据改写案情**：事件日志里，两个旧 Pod 被杀的瞬间，**kubelet 自己的探针**报的是 `context deadline exceeded (awaiting headers)`——等不到响应头。kubelet 探活直连 Pod IP，不经过 Service/kube-proxy/iptables，这条证据把路由竞态**整体排除**：Pod 活着，但连一个 healthz 都答不出来。方向转向资源饥饿。

**升级测量再复测**：给探针加上 `time_total`（区分"连接被拒"和"超时"两种失败特征），复测——0 失败。故障无法稳定复现了。

**强制复现**：给两台 worker 压 CPU 负载再发版。1 核/节点：0 失败。压满 2 核/节点（等于把节点 CPU 全部吃光）：仍然 0 失败。**在集群内能施加的最狠负载下复现不出。**

### 结案陈词

唯一没被排除也无法从虚拟机内部施加的变量：**宿主机层**。三台 VM 共享一台笔记本的物理 CPU，多台 VM 的突发叠加宿主机后台负载（甚至降频）时，vCPU 调度时间被掐——这种饥饿发生在 hypervisor 层，VM 内压测模拟不了。

诚实的结论：**发布窗口内残余尾延迟 0~0.16%，疑似宿主机级瞬时资源饥饿，无法在集群内复现；已做廉价加固（preStop 保留、cpu limit 500m→1 核减少 CFS 节流毛刺），留下带分类能力的探针工具持续观测，复发时按失败特征定罪。**

### 三种失败特征速查表（本次最硬的收获）

| 特征 | 含义 | 指向 |
|---|---|---|
| `code=000, time≈0.01s`（RST） | 包到了，没人监听 | 路由残留 → preStop 修 |
| `code=000, time≈3.0s`（超时） | 没有回应 | 资源饥饿/丢包 |
| kubelet 探针 `awaiting headers` | 进程活着但答得慢 | Pod/节点 CPU 饥饿 |

### 方法论沉淀

1. **修复无效时先验证修复在跑**，再怀疑归因；
2. **找不经路由层的独立证据**（kubelet 直连探针）做交叉验证；
3. **症状相同≠病因相同**，测量细节（耗时、错误类型）是鉴别手段；
4. **单次测量是噪音**：B 轮 4 失败和 C 轮 0 失败之间隔着一个随机变量——这就是 SLO 用窗口统计而非单次压测下结论的原因；
5. **罕见尾延迟不必死磕**：界定（0~0.16%）→ 廉价加固 → 边缘吸收（ingress 重试）→ 留观测待复发。讲一个"没完全解决但已界定、已加固、可观测"的故障，比编一个完美结局更能体现生产判断力。

## 四、坏版本发布 + 回滚实战

故意上不存在的镜像 tag 模拟事故：

```bash
kubectl set image deployment/kaiops-chat chat=docker.io/library/kaiops-chat:v0.99
```

结果：`rollout status` 卡住超时（预期），v0.99 Pod `ImagePullBackOff`，但**旧 Pod 照常服务，探针零失败**——坏版本从没 Ready 过，从没进过 endpoints。**发布失败 ≠ 服务故障**，这就是 maxUnavailable=0 + Readiness 门槛的意义。

回滚：

```bash
kubectl rollout undo deployment/kaiops-chat
```

盯着 `kubectl get rs -w` 看：坏版本 RS 的 DESIRED 跌回 0，上一个 RS 从 0 涨到 2。**回滚不是"恢复数据"，就是一次方向朝回的滚动更新**；那些 DESIRED=0 的 RS 就是 Deployment 保存的历史版本存档，镜像还在节点上，所以回滚比首发快得多。

## 面试考点清单

- readiness vs liveness：前者管"该不该接活儿"（摘流量不重启），后者管"死没死"（重启）；坏版本被挡在门外的机制
- maxSurge / maxUnavailable 的语义与取舍（本文设 1/0：牺牲发布速度换零不可用）
- 回滚原理：RS 存档 + 反向滚动
- Pod 删除完整时序：endpoints 摘除 → preStop → SIGTERM → 宽限期 → SIGKILL（preStop 与 SIGTERM 并行计时，宽限期必须大于 preStop 时长）
- 环境变量注入冲突（enableServiceLinks）、镜像 tag 不可变、Secrets 的同步区隔离
- 同症不同病的鉴别诊断、无法复现故障的四步处理法

## 下一步

Day 8：RAG——Qdrant 向量库 + embedding，把本项目的踩坑文档喂给应用，做一个"运维知识库问答"接口 `/ask_docs`。

---
*本文是 KaiOps 项目（在 K8s 上部署 AI 应用 + 用 AI 做运维）的搭建记录之一。*
