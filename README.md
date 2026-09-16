# KaiOps — AI 应用的云原生运维平台

一个"做出来就能写进简历"的 SRE 练手项目：**把一个接入大模型的 AI 对话应用部署到自建 K8s 集群，配齐可观测性和 CI/CD，再用 LLM 反过来做 AIOps 告警分析**。既是"运维 AI 应用"，也是"用 AI 做运维"。

## 架构（随进度更新）

```
                        ┌────────────────── K8s 集群 (kubeadm v1.34) ──────────────────┐
                        │                                                                  │
用户 ── NodePort/Ingress ─▶ kaiops-chat (FastAPI ×2) ──▶ DeepSeek API                   │
                        │        │            │                                            │
                        │      MySQL       Redis          Flannel / containerd           │
                        │                                     ▲                        │
                        └─────────────────────────────────────┼────────────────────────┘
                                                              │
              Prometheus ◀── exporter ────────────────────────┘
              Grafana / Loki / Alertmanager ──▶ AIOps 机器人(LLM 分析告警) ──▶ 钉钉
```

## 当前进度

- [x] **Day 1~2**：三节点集群底座（Rocky 10 ×3，静态 IP、主机名、时间同步、免密 SSH）
- [x] **Day 3**：kubeadm 搭建 v1.34 集群 + Flannel（[踩坑实录](docs/day3-k8s-pitfalls.md)）
- [x] **Day 4~5**：AI 对话应用容器化部署，NodePort 30080 对外服务（[部署记录](docs/day4-app-deployment.md)）
- [x] **Day 6~7**：MySQL 对话历史 + Redis 会话缓存 + 零停机/回滚实战（[实验全记录](docs/day7-zero-downtime.md)）
- [ ] **Day 8（进行中）**：RAG——Qdrant 向量库 + embedding + `/ask_docs` 知识库问答
- [ ] **Day 9**：ingress-nginx + 域名 chat.kaiops.local
- [ ] **Week 3**：kube-prometheus-stack 监控 + 自定义 exporter
- [ ] **Week 4+**：GitLab CI + Harbor + Argo CD（GitOps）
- [ ] **Month 2**：AIOps 告警分析机器人

## 环境

| 节点 | IP | 配置 | 角色 |
|---|---|---|---|
| k8s-master | 192.168.29.130 | 2C4G | control-plane |
| k8s-node1 | 192.168.29.131 | 2C4G | worker |
| k8s-node2 | 192.168.29.132 | 2C4G | worker |

## 目录结构

```
kaiops/
├── app/                 # AI 对话应用（FastAPI + DeepSeek）
├── deploy/              # K8s 部署清单
│   └── 01-secret.yaml   # 真实密钥版（gitignore，只存在于 master）
├── docs/                # 搭建记录 / 踩坑实录
└── Dockerfile
```
