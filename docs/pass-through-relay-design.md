# LiteLLM Pass-Through 出站鉴权中转服务设计

## 状态

首版已按本文架构实现，运行与配置方法见 [README](../README.md)。LiteLLM 侧行为仍需在实际部署版本进行端到端验证。

## 背景与目标

LiteLLM Pass-Through 适合做统一入口和用户密钥鉴权。但客户端请求中的 `Authorization` 既可能承载 LiteLLM 用户密钥，也可能是目标 API 要求的上游凭证。直接透传时，这两类凭证会发生冲突。

本设计增加一个运行在本机 `:3000` 的轻量 HTTP 中转服务。LiteLLM 继续负责入口鉴权；中转服务负责依据路由选择真正的上游，并将专用的上游凭证头改写为上游要求的 `Authorization`。

目标：

- 不改变 LiteLLM 的全局鉴权头配置。
- LiteLLM 仍负责验证调用方的 LiteLLM 用户密钥。
- 客户端用独立的 `pt-api-key` 请求头提供上游凭证。
- 中转服务将 `pt-api-key` 改写为发往目标 API 的 `Authorization`。
- 支持通过路由配置维护上游地址和路径转发规则。

## 请求链路

```text
客户端
  Authorization: Bearer <LiteLLM 用户密钥>
  pt-api-key: <上游 API 凭证>
       |
       v
LiteLLM Pass-Through
  1. 验证 Authorization 中的 LiteLLM 用户密钥
  2. 将请求转发到本机中转服务
       |
       v
中转服务 :3000
  1. 根据本地路由配置选择上游
  2. 读取 pt-api-key
  3. 设置上游 Authorization = pt-api-key 的值
  4. 删除 pt-api-key 和任何 LiteLLM 凭证头
  5. 转发请求并返回上游响应
       |
       v
目标 API（例如 api.deepseek.com）
```

客户端的 `Authorization` 是 LiteLLM 入口凭证；目标 API 的凭证单独放在 `pt-api-key`。中转服务发往目标 API 的 `Authorization` 必须来自 `pt-api-key`，不能回退使用 LiteLLM 凭证。

## LiteLLM 侧配置示意

以下为配置意图示例，具体字段支持情况应根据实际使用的 LiteLLM 版本核对：

```yaml
general_settings:
  pass_through_endpoints:
    - path: "/deepseek"
      target: "http://127.0.0.1:3000/deepseek"
      auth: true
      forward_headers: true
      include_subpath: true
```

LiteLLM 只将 Pass-Through 请求交给本机中转服务。它不需要更改全局 `litellm_key_header_name`，也不负责把上游凭证写入其自身的静态配置。官方文档将自定义端点的 `auth: true` 标为 Enterprise 功能；实际部署必须确认入口鉴权生效。

## 中转服务配置示意

中转服务维护独立的路由表。首版使用以下配置：

```yaml
listen: "127.0.0.1:3000"
routes:
  - prefix: "/deepseek"
    upstream: "https://api.deepseek.com"
    strip_prefix: true
    timeout_seconds: 600
```

首版固定使用 `pt-api-key` 到 `Authorization` 的映射，并支持每条路由独立超时。`gateway_auth_headers` 可配置额外需要清理的 LiteLLM 鉴权头。可选的后续扩展包括为每条路由定义允许的 HTTP 方法和静态上游头。

## 头部处理规则

1. 中转服务要求配置指定的凭证源头（首版为 `pt-api-key`）；缺失时返回明确的客户端错误，不发送未认证的上游请求。
2. 将该值原样设为上游 `Authorization`，保留调用方提供的认证 scheme（例如 `Bearer ...`）。
3. 发往上游前移除 `pt-api-key`，避免专用凭证头泄漏。
4. 发往上游前移除 `Authorization`、`x-litellm-api-key`、配置的 LiteLLM 自定义鉴权头等网关凭证头；再以 `pt-api-key` 的值设置目标 `Authorization`。
5. 不把 LiteLLM 入口密钥用作上游凭证，也不在日志中记录任何完整凭证。
6. 对重复的 `pt-api-key` 头应拒绝请求，避免不同 HTTP 组件对重复头采用不同解析结果。

## 路径与请求转发

- 路由匹配使用最长前缀优先，避免更宽泛的路由遮蔽更具体路由。
- 保留原始 HTTP 方法、请求体和非敏感请求头。
- 明确配置上游路径是否保留前缀；首版应保持输入路径与上游路径的可预测对应关系。
- 转发常见请求头，如 `Content-Type`、`Accept` 和流式响应相关头；过滤 hop-by-hop 头以及 `Host`、`Content-Length` 等需由 HTTP 客户端重新生成的头。
- 透传上游状态码、响应体及必要的响应头；对 SSE/流式响应采用流式转发，避免缓冲完整响应。
- 保留查询参数；如后续需要默认参数或覆盖规则，再作为独立能力设计。

## 网络与信任边界

- 默认只监听 `127.0.0.1:3000`。如果 LiteLLM 和中转服务运行在不同容器，应改用仅容器网络可达的地址，并通过网络策略限制访问。
- 中转服务不应暴露到公网。LiteLLM 负责用户鉴权，但若中转服务可被绕过直接访问，攻击者可能跳过 LiteLLM 的入口鉴权。
- 如果部署环境无法保证只有 LiteLLM 能访问中转服务，应增加 LiteLLM 到中转服务之间的服务级鉴权或网络层访问控制。
- 上游地址来自受信任的本地路由配置，不从请求头或请求参数动态解析，以降低 SSRF 风险。
- 不记录 `Authorization`、`pt-api-key` 或 LiteLLM 密钥；错误日志应遮蔽 URL 中可能包含敏感数据的查询参数。

## 失败行为

- 未匹配路由：返回 `404`，不尝试任意上游地址。
- 缺少或重复凭证头：返回 `400`，不发起上游请求。
- 上游连接、超时或协议错误：返回适当的 `502`/`504`，日志记录路由标识和错误类别，不记录凭证。
- 上游 HTTP 错误响应：保留上游状态码和响应体，避免把有效的 API 错误改写为中转服务错误。

## LiteLLM 版本兼容性验证

部署前应在目标 LiteLLM 版本验证以下行为：

1. Pass-Through 请求能否携带并转发 `pt-api-key`。
2. `forward_headers` 对自定义头的实际行为。
3. LiteLLM 完成入口鉴权后，是否会把其自身的 `Authorization` 或其他网关密钥头转发给 Pass-Through 目标。
4. 带子路径和查询参数的请求是否按预期到达中转服务。
5. SSE/流式响应能否端到端保持流式。

无论 LiteLLM 是否剥除入口密钥，中转服务都应在出站前强制清理网关凭证头，并以 `pt-api-key` 覆盖目标 `Authorization`。这是防止上游收到 LiteLLM 用户密钥的最后一道边界。

## 首版范围

- 本机 HTTP 服务，监听地址可配置，默认仅绑定 loopback。
- 静态路由配置：前缀、上游 base URL、路径处理策略、超时。
- `pt-api-key` 到上游 `Authorization` 的映射。
- 请求/响应透传，包括 SSE 流式响应。
- 基本访问日志与凭证脱敏。
- 不实现 LiteLLM 用户密钥校验；入口校验由 LiteLLM 完成。
- 不实现动态上游 URL、复杂 OAuth 刷新、请求体改写或完整反向代理管理面板。

## 部署前需要确认

- 首版固定使用 `pt-api-key`；确认调用方可以提供该头。
- 路由按 `strip_prefix` 决定是否移除 `/deepseek` 等前缀；确认目标 API 的路径要求。
- LiteLLM 与中转服务的实际部署方式（同一主机、Docker Compose 或 Kubernetes），以确定监听地址和服务间访问控制。
- 是否需要中转服务自身的健康检查端点及最小化指标。
