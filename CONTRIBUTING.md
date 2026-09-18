# 开发与测试

使用 Python 3.10+、FFmpeg 和 Node.js 20+。核心运行不需要 Node.js；它只用于浏览器测试。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e . build
npm ci
npx playwright install chromium
python -m unittest discover -s tests -v
npm test
python -m build
```

Linux 缺少浏览器系统依赖时使用 `npx playwright install --with-deps chromium`。可通过 `PYTHON` 指定测试子进程的 Python，通过 `PLAYWRIGHT_EXECUTABLE_PATH` 指定已安装的 Chromium。这些机器相关路径放在环境变量中，不写入仓库。

核心测试实际调用 FFmpeg，包括第二个独立项目、多原片、静音视频、分数秒剪辑、字幕纠错、旧版本保留、决定冲突、原片哈希与受保护的 HTTP 路由。浏览器测试创建临时合成项目，完成试听、保存、刷新、生成、下载、导入导出、跨页冲突及 320/390/768/1440 宽度检查。测试结束移除自己创建的临时项目。

测试截图与结果写入被忽略的 `.working/qa/`。更新公开 README 截图：

```sh
UPDATE_SCREENSHOT=1 npm test
```

它只截图测试生成的合成项目。不要用私人录制替换公开截图。

## 提交前

行为变更附能验证用户可观察结果的测试。新增剪辑策略不应把 AI 的判断写成人工确认。涉及文件格式时保留版本标识，并解释已有项目怎样迁移。

验证包可以安装，检查 `git status` 与即将提交的文件清单。默认忽略 `projects/`、`private/`、`.working/`、媒体文件、环境文件与构建目录；放在其他目录的 JSON 转写仍需人工审查。

公开问题请描述输入类型、版本、复现步骤、预期与实际结果。使用合成素材复现，避免附上真人原片、访问链接口令或含隐私的项目快照。
