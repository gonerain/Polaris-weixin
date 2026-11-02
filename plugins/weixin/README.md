# 微信组件

提供扫码绑定、文字收发、可选的自动回复和画布聊天视图。已绑定时启动组件自动连接并接收消息，未绑定时等待扫码。点击“扫码绑定”，使用手机微信扫码并确认；如提示配对码，输入手机显示的码继续。

## 运行

在仓库根目录使用现有 Python 环境：

```sh
python -m pip install -r plugins/weixin/requirements.txt
PYTHONPATH=next python -m polaris_platform --gui --config next/config.weixin.json
```

也可通过组件管理器加载 `plugins/weixin` 并启动，再在画布选择“微信”，确认权限后挂载。图形界面需要 PySide6，二维码本地绘制需要 `qrcode`。关闭或移除视图保留通信，停止组件结束通信。

绑定者先发一条文字消息后，本机可以回复，每条最多 2000 字。发送未确认时显示“发送结果未知”，不会自动重发；可在手机微信核实结果。

## 自动回复

微信可独立扫码、收发和显示聊天。自动回复使用同一提供方的 `polaris.agent.reply@1` 和 `polaris.agent.status@1` 能力。启动 Polaris Agent 并配置其模型后，在微信视图中开启自动回复。微信只转交新消息，用发送成功的回执记录已送达轮次。

“设置”默认每个组件一行，可一键选择该组件当前全部公开操作；需要细分时展开“自定义”，逐项选择查看或操作。点击本机“保存”即授权所选能力，无需再次确认。权限授予微信组件自身，取消勾选并保存会立即撤销对应能力，旧任务也不能继续调用。打开设置才读取目录，关闭设置保留未保存的勾选。“新对话”切换到全新的对话；退出或换账号也会重置上下文。

允许列表和自动回复开关保存在本机。开启后，将必要权限保存为微信自身的可恢复任务；重启和重连后恢复，无需打开视图。每次调用仍检查当前授权，撤销的权限不会在轮询或重启时自动补回。关闭自动回复会删除恢复任务，不会重新授予所选业务能力。旧设置需要在本机重新点击一次“保存”，以记录微信自身的授权与任务。保存失败会显示错误并保留原选择与授权。

授权只由本机原生视图中的明确保存或开启动作发起；普通 `configure_agent` 调用只能使用调用方已有权限，不能自行授权。授权不足、密钥缺失或发送结果未知会显示错误，不会伪装为运行中。发送给 DeepSeek 的内容包括新收到的文字及工具结果。

## 能力接口

`get` 返回状态、提示、账号标识、二维码内容、发送状态及最近消息；每条消息包含 `id`、`text`、`direction`、`status`、`time`。

写操作为 `login`、`verify(code)`、`connect`、`disconnect`、`logout`、`send(text)`、`clear_messages`。`main` 视图使用通用 `qt-quick-item` 契约，通过 `create_view` / `release_view` 创建与释放。`agent_settings` 返回自动回复状态、回复提供方是否可用和渠道允许列表；`agent_catalog` 返回公开操作元数据；`configure_agent(enabled, allowed)` 设置本轮启用状态和可选允许列表；`new_conversation` 开始新的回复上下文。业务接口不依赖 Qt。撤销读取权限后视图清除账号和消息展示；写入操作也会重新校验授权。

## 本地数据

默认数据目录：Windows 为 `%LOCALAPPDATA%/polaris-weixin`，Linux 为 `${XDG_DATA_HOME:-~/.local/share}/polaris-weixin`；入口支持 `data_dir` 指定独立目录。

Windows 使用 DPAPI 加密保存 `credentials.bin`；Linux 使用本地 JSON 保存 `credentials.bin`，以 `0600` 文件权限限制为当前用户读写，不加密。保存内容包括账号凭证、绑定标识、接收进度和最近 512 条消息的去重标识，不保存会话 context token 或临时二维码。重启后恢复绑定和聊天记录，启动组件自动连接，无需再次扫码；手动断开后可直接点击卡片上的“连接”；保存失败会显示提示。损坏、过大或版本不支持的凭证文件会保留，退出绑定后可重新保存。`chats/` 内为按账号及绑定用户隔离的 JSON 聊天记录，每组最多保留最近 100 条。清除消息删除当前记录；退出绑定清除当前账号凭证和聊天记录。

自动测试使用临时目录、虚构账号和假网络，不会发送真实微信消息；界面测试在独立 Qt 子进程中运行，未安装 PySide6 时跳过。

`integration_tests/test_weixin_agent_e2e.py` 使用实际微信组件、Polaris Agent、Agent Bridge、平台权限和本地 socket 桥，覆盖多轮标记/取消、发送去重、投递确认、结果读取、离线、权限撤销和长结果。`test_weixin_canvas.py` 同时挂载两个真实组件，检查微信操作后 Agent 视图同步刷新。

设置 `POLARIS_WEIXIN_LIVE_MODEL=1` 和已有的 `DEEPSEEK_API_KEY` 可启用六轮真实模型验收；运行 `PYTHONPATH=next python integration_tests/test_weixin_agent_e2e.py WeixinAgentEndToEndTests.test_real_model_multiturn_queries_marks_sends_and_unmarks -v`。这项验收调用真实 DeepSeek，仅把合成会话数据发给模型，微信网络和代理宿主使用隔离测试端，不会向真实联系人或工作会话发送测试消息。

自动回复会在发送前用本轮实际工具结果核对回答，发现缺少执行证据时继续调用工具，仍无法核实时明确报错。`pending` 只表示提交等待确认，`accepted` 只表示接收方已接收。工具预算结束后预留最终回复；模型失败时告知已确认的操作和未确认的结果，避免重复提交。工具结果以 JSON 交给回复提供方。

模型请求、提示词、对话上下文和工具执行均由回复提供方维护。

任务结束通知通过可选适配器处理：遇到声明 `completionCallback` 的发送操作，微信自动附加本组件的结果回传地址，并记录原微信账号、投递和会话。对应轮次确定结束后直接发送状态和最终正文，不调用 DS。相同会话、轮次只通知一次；桌面直接发起且没有回传地址的任务不会发送微信通知。关闭或未安装适配目标不影响微信自身通信；关闭微信也不影响任务提供方运行。

`complete_agent_run` 是适配器的回传入口，校验原任务、调用方和账号；需要随自动回复任务保存该操作权限。通知记录保存在 `agent-completions.json`，重启保留去重信息。微信暂不可发送时等待恢复；网络发送结果未知时保留消息状态，不自动重复发送。完整链路与组件独立性测试位于 Agent Bridge 仓库的 `integration_tests/test_agent_completion_adapter.py`，使用隔离传输，不调用模型。
