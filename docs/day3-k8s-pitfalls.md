# kubeadm + Rocky Linux 10 搭建 K8s 集群踩坑实录（containerd 2.x 三个经典坑）

> 环境：Windows 11 + VMware + Rocky Linux 10.2 ×3（2C4G）｜kubeadm v1.34.11｜containerd 2.3.5（docker-ce 源的 containerd.io 包）
>
> 这是我 SRE 学习项目的 Day 3：目标是搭一个三节点 K8s 集群。集群最终搭好了，但过程中踩的三个坑都很有代表性，特别是 containerd 2.x 配置格式的变化——网上大部分教程还是 1.x 时代的，照抄必踩。记录下来，一是给自己留档，二是希望帮到同样在搭集群的人。

## 最终架构

| 节点 | IP | 角色 |
|---|---|---|
| k8s-master | 192.168.29.130 | control-plane |
| k8s-node1 | 192.168.29.131 | worker |
| k8s-node2 | 192.168.29.132 | worker |

网络插件 Flannel（10.244.0.0/16），containerd 运行时（SystemdCgroup 驱动），镜像走国内加速。

---

## 坑 1：containerd 2.x 配置格式大改——`sandbox_image` 已经不存在了

### 现象

按老教程执行：

```bash
containerd config default > /etc/containerd/config.toml
sed -i 's#registry.k8s.io/pause:3.8#registry.aliyuncs.com/google_containers/pause:3.9#' /etc/containerd/config.toml
```

结果 containerd 起不来，journalctl 里是 TOML 解析错误：

```
containerd: failed to unmarshal TOML at row 28 column 68: toml: literal strings cannot have new lines
```

### 根因

两个问题叠加：

**① 配置版本变了。** containerd 2.3.5 生成的配置文件是 `version = 4`，pause 镜像的配置项**改名了**，老的 `sandbox_image` 键不存在，现在长这样（注意还换了层级，挪进了 `pinned_images`）：

```toml
[plugins.'io.containerd.cri.v1.images'.pinned_images]
  sandbox = 'registry.k8s.io/pause:3.10.2'
```

所以按老键名写的 sed 静默地什么都没改（sed 不报错，这是最阴的地方）。

**② 引号风格变了。** 2.x 的模板用**单引号**（TOML literal string），老教程里 `[^"]*` 这种按双引号写的正则会把行尾的单引号一起吃掉，直接把配置文件改坏——上面 row 28 的报错就是 eaten quote 导致的。

### 正确写法

```bash
# 只换仓库前缀，保留官方版本号，单引号安全
sed -i "s#sandbox = 'registry.k8s.io/#sandbox = 'registry.aliyuncs.com/google_containers/#" /etc/containerd/config.toml
```

### 教训

**改配置文件之前，先 `grep -n` 看一眼原文的键名和引号风格；改完再 grep 验证。** sed 不匹配时不会报任何错，你以为改了，其实什么都没发生。

另外提一句：containerd 2.x 的 `SystemdCgroup` 默认是 `false`，kubelet 默认用 systemd 驱动，两边必须一致，否则 kubelet 起来就报 `misconfiguration`：

```bash
sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
```

---

## 坑 2：重新生成配置文件 = 丢掉之前所有手工修改

### 现象

坑 1 修好后我又翻车了一次：为了修复被 sed 改坏的文件，我重新跑了 `containerd config default > config.toml`，服务起来了，以为万事大吉。直到按验收步骤 grep 才发现：

```
$ grep SystemdCgroup /etc/containerd/config.toml
            SystemdCgroup = false     # 之前明明改成 true 了！
```

### 根因

`containerd config default > /etc/containerd/config.toml` 是**整体覆盖**，会把你之前所有的手工修改（SystemdCgroup、镜像加速、pause 地址）全部冲掉。文件是"新"的，但也是"裸"的。

### 教训

- 重新生成配置后，**所有修改项必须重打一遍**，并且逐项 grep 验收；
- 每次改配置，三件套不能省：`改前 grep 看原文 → 改 → 改后 grep 验收`。

---

## 坑 3：`systemctl enable --now` 不会重启已运行的服务——最长的一次排查

这个坑最隐蔽，表象和根因隔了三层。

### 现象

node2 上执行 `kubeadm join`，报了一堆错：

```
W: validate CRI v1 runtime API ... rpc error: code = Unimplemented
    desc = unknown service runtime.v1.RuntimeService
[ERROR FileAvailable--etc-kubernetes-kubelet.conf]: /etc/kubernetes/kubelet.conf already exists
[ERROR FileAvailable--etc-kubernetes-bootstrap-kubelet.conf]: ... already exists
[ERROR FileAvailable--etc-kubernetes-pki-ca.crt]: ... already exists
```

`kubectl get nodes` 里也看不到 node2，但 node2 上 kubelet 一直处于 `activating`（无限重启）。

### 排查过程

**第一层：文件已存在 ≠ join 失败。** `kubelet.conf` 只有 join 成功后才会生成——说明 node2 其实早就 join 过了，这是重复执行。真正的问题是它为什么没在集群里注册上。

**第二层：CRI 服务为什么 unknown？** `ctr plugins ls | grep cri` 一看，node2 上 CRI 相关插件只有两个 ok，暴露 gRPC 服务的关键插件 `io.containerd.grpc.v1 cri` 压根不在列表里。kubelet 连不上 CRI，所以无限崩溃重启，节点自然注册不上。

**第三层：插件为什么没起来？** 磁盘上的配置文件三个修改项全对（grep 验证过）。那看运行时——containerd 启动日志里有一行 `starting cri plugin`，后面跟着它**实际加载的完整配置 JSON**，在里面搜 SystemdCgroup：

```
"options":{"...","SystemdCgroup":false}
```

磁盘上是 `true`，运行时是 `false`——**守护进程加载的是修改前的旧配置**。

### 根因

时间线还原：RPM 安装 containerd.io 时服务就被拉起来了（加载了当时的旧配置）→ 之后我修好了磁盘上的配置文件 → 收尾用的 `systemctl enable --now containerd` 对**已经在运行**的服务只会 enable，**不会重启** → 守护进程一直揣着旧配置跑了四十分钟。

```bash
systemctl enable --now containerd   # 已运行时：只做 enable，不重启（坑！）
systemctl restart containerd        # 改配置后必须用这个
```

### 修复

```bash
systemctl restart containerd    # 三个 CRI 插件全部 ok
systemctl restart kubelet       # kubelet 稳定 active
kubectl get nodes               # 9 秒后 node2 出现在集群里
```

不需要重新 join——bootstrap 凭据早就生成好了，kubelet 通了 CRI 自然就注册上了。

### 教训

这是本次最有面试价值的一条：**改了配置文件 ≠ 生效**。判断依据不是磁盘上的文件，而是运行时实际加载的配置。很多服务启动时会打印生效配置（containerd 的 `starting cri plugin`、nginx 的 `nginx -T`、`sysctl` 值），排查"改了没效果"类问题，第一步就是对比这两者。

---

## 坑 4（附赠）：raw.githubusercontent.com 被墙

装 Flannel 时 `curl raw.githubusercontent.com/.../kube-flannel.yml` 超时。绕路方案：jsDelivr 的 CDN 镜像，把

```
https://raw.githubusercontent.com/<owner>/<repo>/<branch>/<path>
```

换成

```
https://cdn.jsdelivr.net/gh/<owner>/<repo>@<branch>/<path>
```

实测可用。

---

## 坑 0（前情提要）：克隆虚拟机的两个经典雷

这次 Day 2 还踩了两个更基础的，一并记录：

1. **克隆出来的虚拟机主机名相同**——K8s 要求节点主机名唯一，`hostnamectl set-hostname` 逐台改；
2. **IP 是 DHCP 租的**——K8s 节点 IP 必须固定，否则重启后 IP 一变整个集群失联。Rocky 10 已废弃 `ifcfg-*` 文件，用 nmcli 改静态：

```bash
nmcli con mod ens160 ipv4.method manual \
  ipv4.addresses 192.168.29.131/24 \
  ipv4.gateway 192.168.29.2 \
  ipv4.dns '223.5.5.5 114.114.114.114'
nmcli con up ens160
```

（VMware NAT 的网关默认是网段的 `.2`，不是 `.1`，猜错上不了网。）

---

## 方法论总结

1. **改配置三件套**：改前 grep 看原文（键名 + 引号）→ 改 → 改后 grep 验收。sed 静默失败是最常见的坑。
2. **配置生效的判据是运行时，不是磁盘**：containerd 看 `starting cri plugin` 日志、nginx 用 `nginx -T`、内核看 `sysctl -n`。
3. **`enable --now` 不是 restart**：改完配置要重启服务，老老实实 `systemctl restart`。
4. **报错要读全再动手**："file already exists"指向的是"重复 join"这个表象，真正的病根在 CRI 插件；顺着表象修（比如删文件重 join）只会把 join 也搞坏。
5. **每一步留验收命令**：这次好几个坑都是验收 grep 抓出来的，而不是靠肉眼读配置。

---

*本文是 KaiOps 项目（在 K8s 上部署 AI 应用 + 用 AI 做运维）的搭建记录之一，后续会持续更新应用部署、监控、CI/CD、AIOps 各阶段。*
