# LiteLLM Pass-Through 鉴权中转

FastAPI 服务负责将客户端的 `pt-api-key` 映射为目标 API 的 `Authorization`。LiteLLM 负责验证客户端的 LiteLLM 密钥，中转服务负责上游路由与出站头清理。

## 启动

要求 Python 3.10+。

```powershell
python -m pip install -e .
Copy-Item relay.example.yaml relay.yaml
python -m litellm_passthrough --config relay.yaml
```

默认监听 `127.0.0.1:3000`。`relay.yaml` 中每条路由配置 `prefix`、`upstream`、`strip_prefix` 和 `timeout_seconds`。`strip_prefix: true` 会把 `/deepseek/v1/chat/completions` 转为上游 `/v1/chat/completions`；设为 `false` 则保留 `/deepseek`。支持最长前缀匹配。`gateway_auth_headers` 可列出部署中额外使用的 LiteLLM 鉴权头，出站时会删除它们。

`GET /healthz` 返回服务健康状态。服务不记录完整 URL 或凭证，且默认关闭 Uvicorn 的访问日志，以免查询参数进入日志。

### Docker Compose

先复制路由配置，并将 `relay.yaml` 中的 `listen` 改为 `"0.0.0.0:3000"`，同时按需修改上游路由：

```powershell
Copy-Item relay.example.yaml relay.yaml
# 编辑 relay.yaml：listen: "0.0.0.0:3000"
docker compose up --build -d
docker compose ps
curl.exe http://127.0.0.1:3000/healthz
```

`Dockerfile` 构建仅包含本服务运行依赖的镜像；Compose 将本地 `relay.yaml` 只读挂载到容器，并检查 `/healthz`。配置文件改动后运行 `docker compose restart relay`；代码或依赖改动后运行 `docker compose up --build -d`。查看日志用 `docker compose logs -f relay`，停止用 `docker compose down`。

Compose 只把端口发布到宿主机 `127.0.0.1:3000`。LiteLLM 运行在宿主机时，下面的 `target` 可直接使用。LiteLLM 也运行在容器中时，应使它与 `relay` 服务加入同一 Docker 网络，并将 `target` 改为 `http://relay:3000/deepseek`；容器内的 `127.0.0.1` 指向容器自身。只让可信的 LiteLLM 容器访问中转服务，不要把中转服务端口直接发布到公网。

LiteLLM 的 `config.yaml` 中加入：

```yaml
general_settings:
  pass_through_endpoints:
    - path: "/deepseek"
      target: "http://127.0.0.1:3000/deepseek"
      auth: true
      forward_headers: true
      include_subpath: true
```

请求示例（LiteLLM 默认端口为 `4000`）：

```powershell
curl.exe -N http://127.0.0.1:4000/deepseek/v1/chat/completions `
  -H "Authorization: Bearer <LiteLLM 用户密钥>" `
  -H "pt-api-key: Bearer <DeepSeek API 密钥>" `
  -H "Content-Type: application/json" `
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"hello"}],"stream":true}'
```

注意：`pt-api-key` 的值会原样写入上游 `Authorization`，因此需要包含上游要求的 scheme，例如 `Bearer `。默认只监听本机；若 LiteLLM 在另一个容器，使用私有容器网络地址，并限制外部直接访问中转服务。不要把中转服务直接暴露到公网。

[LiteLLM 官方文档](https://docs.litellm.ai/docs/proxy/pass_through)将自定义 Pass-Through 端点的 `auth: true` 标为 Enterprise 功能。若当前部署不支持该配置，不能假定 LiteLLM 已完成入口鉴权；需先使用支持该能力的版本或另设可信的入口鉴权层。LiteLLM 不在此项目的依赖中。上线前须在实际 LiteLLM 版本上验证用户密钥鉴权、`pt-api-key` 头转发、子路径及 SSE；中转服务自身的单元测试不能证明 LiteLLM 侧行为。

运行测试：

```powershell
python -m pip install -e '.[test]'
python -m pytest -q
```
