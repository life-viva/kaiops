# Day 4~5：把一个 AI 应用部署上自建 K8s 集群（podman 构建 + containerd 分发）

> 承接 [Day 3 集群搭建](day3-k8s-pitfalls.md)。本篇完成项目的第一个里程碑：**一个接入大模型的对话应用，从代码到镜像到 K8s 集群对外服务，全流程跑通**。应用很朴素（FastAPI 调 DeepSeek API），但重点是整条交付链路。

## 最终效果

浏览器访问 `http://<node-ip>:30080/docs`，在 Swagger 页面里调 `/chat` 接口，DeepSeek 秒回——应用跑在自己 kubeadm 搭的集群上，2 副本，过就绪探针才接流量。

## 为什么用 podman，不是 docker？

集群三台机器装的都是 containerd（kubeadm 的标准姿势），没有 docker。构建镜像有三个选择：

1. 装 docker-ce——会和独立 containerd 抢 systemd 单元和 socket，经典的坑源，pass；
2. nerdctl + buildkit——containerd 原生，但二进制要从 GitHub Releases 下载，网络不可控；
3. **podman**——Rocky 官方源直接 `dnf install`，无守护进程、不碰 containerd 的任何状态，构建产物用 `podman save` 导出后 `ctr import` 给 containerd。

选 3。生产上更常见的是"构建机/CI 出镜像 → 推 Registry → 节点拉取"（Week 5 上 Harbor + CI/CD 时会替换成那条路），手工 save/import 只是零 Registry 阶段的过渡方案。

## 镜像命名的一个小知识点

构建时打的是**完整镜像名**：

```bash
podman build -t docker.io/library/kaiops-chat:v0.1 .
```

为什么不用 `kaiops-chat:v0.1`？因为 kubelet 会把短名规范化成 `docker.io/library/kaiops-chat:v0.1`，而 containerd 里导入的镜像名是**精确匹配**的。短名构建 → 镜像在库里叫 `localhost/kaiops-chat` → Pod 报 `ErrImageNeverPull`/`ImagePullBackOff`，而且报错里的名字看起来"明明一样"，极难肉眼发现。**构建、保存、yaml 里三处用同一个全名，一劳永逸。**

## 构建与本地验证

```bash
# podman 的镜像加速（和 containerd 的 certs.d 是两套配置！）
mkdir -p /etc/containers/registries.conf.d
printf '[[registry]]\nprefix = "docker.io"\nlocation = "docker.m.daocloud.io"\n' \
  > /etc/containers/registries.conf.d/cn-mirror.conf

cd /root/kaiops
podman build -t docker.io/library/kaiops-chat:v0.1 .

# 先单机试跑，curl 能回话再上集群
podman run -d --name chat-test -e LLM_API_KEY=sk-xxx -p 18000:8000 docker.io/library/kaiops-chat:v0.1
curl -X POST localhost:18000/chat -H 'Content-Type: application/json' -d '{"message":"你好"}'
```

Dockerfile 里两处细节：pip 走清华源（构建快且稳）；`EXPOSE 8000` 只是文档声明，真正映射靠 K8s 的 Service。

## 分发镜像

```bash
podman save --format docker-archive -o /tmp/kaiops.tar docker.io/library/kaiops-chat:v0.1
ctr -n k8s.io images import /tmp/kaiops.tar                          # master 自己
cat /tmp/kaiops.tar | ssh 192.168.29.131 "ctr -n k8s.io images import -"   # node1
cat /tmp/kaiops.tar | ssh 192.168.29.132 "ctr -n k8s.io images import -"   # node2
```

关键点是 `-n k8s.io`：containerd 有多个命名空间，kubelet 只认 `k8s.io` 这个。导到默认 namespace 的话镜像"明明在"，kubelet 就是看不见——又一个肉眼难辨的坑。

（`save | ssh import` 之前先把 master 到 node 的免密做好：`ssh-keygen` + `ssh-copy-id`。）

## 部署清单的设计

四个 yaml，各自管什么：

| 文件 | 内容 | 值得说的点 |
|---|---|---|
| 00-namespace | kaiops 命名空间 | 资源隔离的基本单位 |
| 01-secret | DeepSeek key | **真实 key 的文件被 .gitignore 挡住，仓库里只有占位模板**——密钥进 git 史是没法撤销的事故 |
| 02-deployment | 2 副本 + 探针 + 资源配额 | `envFrom secretRef` 注入环境变量，代码里只读 `os.environ`；`readinessProbe` 保证没就绪不接流量；requests/limits 让调度器有依据 |
| 03-service | NodePort 30080 | 先用最简单的方式暴露，Week 3 换 Ingress + 域名 |

`imagePullPolicy: IfNotPresent` 配合手工导入的镜像：节点上有就直接用，不去 Registry 拉。

```bash
vim /root/kaiops/deploy/01-secret.yaml    # 填真实 key
kubectl apply -f /root/kaiops/deploy/
kubectl get pods -n kaiops -o wide        # 2 个 Running，分布在两个 worker 上
```

## 验收

```bash
kubectl get svc -n kaiops           # kaiops-chat  NodePort  80:30080
curl -X POST http://192.168.29.131:30080/chat \
  -H 'Content-Type: application/json' -d '{"message":"我是谁"}'
```

浏览器打开 `http://192.168.29.131:30080/docs`，Swagger 页面直接调通——**项目 1.0 上线**。

## 复盘

- 这次全链路里最值得记的是**镜像名一致性**（全名三处统一）和 **containerd 命名空间**（`-n k8s.io`）这两个"名字看起来对但就是不行"的坑；
- 手工 save/import 是权宜之计，Week 5 上 Harbor 后删除这一步；
- 密钥管理从第一天就做对：gitignore + 只在 master 上编辑真实 Secret。

## 下一步（Week 2）

local-path StorageClass → MySQL（对话历史）+ Redis（会话缓存）上集群 → 应用 v0.2 接入两个中间件、体验滚动更新 → RAG（Qdrant + embedding）→ ingress-nginx + 域名 `chat.kaiops.local`。
