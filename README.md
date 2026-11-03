# Polaris 微信

本仓库是 Polaris 插件源，根目录 `polaris-source.json` 声明可安装插件；同一清单可列出多个插件目录。将[本仓库](https://github.com/gonerain/Polaris-weixin)地址或本地目录添加到 Polaris 插件源后，选择安装 `微信`。

开发需要 Python 3.10+、兼容 platformApi 1 的 `polaris-platform` SDK 和 Qt 6.5+。先安装平台 SDK，再安装本仓库开发依赖：

```sh
python -m pip install 'git+https://github.com/gonerain/Polaris-desktop.git#subdirectory=next'
python -m pip install -r requirements.txt
PYTHONPATH=plugins/weixin python -m unittest discover -s tests
```

原生 Qt 界面使用宿主的 Python/Qt 环境。业务数据仍由组件自己的默认数据目录保存，更新源码不会移动数据。

配置示例：`config.weixin.json`，从本仓库根目录使用 Polaris CLI 加载。测试使用已安装的公开 SDK；桌面端到端测试另外使用平台公开桌面宿主。

二维码编码器 qrcode 8.2 随组件携带，安装源不需要修改宿主依赖。可选跨仓测试：设置 `POLARIS_AGENT_REPO` 为 Agent checkout 后运行 `python -m unittest discover -s integration_tests`。

本地平台开发可用 `python -m pip install -e /path/to/Polaris/next` 代替 Git 安装。

自动回复通过公开的 `polaris.agent.reply@1` 对话服务委托处理。组件保存自动回复开关、允许操作、会话投递和账号绑定，不读取模型 API Key。
首次使用时选择允许操作并开启自动回复；模型就绪状态由对话服务提供。
