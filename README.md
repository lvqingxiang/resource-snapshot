# web-resource-snapshot

这是一个独立的小工具。

当前版本输入一条 X / Twitter 推文链接后，会自动打开详情页截图，并把图片保存到当前文件夹下的 `screenshots/` 目录。
之所以使用更通用的命名，是为了后续扩展到其他网页资源快照。

如果推文里包含视频，现在也支持填写一个可选时间点，尽量截到你想要的画面。
如果帖子正文不是中文，可以勾选“追加正文中文翻译”，让图片里同时保留原文和中文翻译。

## 目录说明

- `app.py`: 本地网页服务
- `screenshot_service.py`: Playwright 截图逻辑
- `static/index.html`: 输入链接的网页
- `screenshots/`: 保存生成的截图
- `browser_profile/`: 浏览器登录信息缓存

## 启动方式

### 方式一：双击启动

双击同目录下的 `start_resource_snapshot_tool.command`。
如果服务已经在运行，脚本不会重复启动，而是直接提示访问地址：

```text
检测到服务已在运行，无需重复启动。
直接访问: http://127.0.0.1:5080
```

### 方式二：命令行启动

```bash
cd /Users/lv/Desktop/resource_snapshot_tool
python app.py
```

启动后会自动打开浏览器：

```text
http://127.0.0.1:5080
```

## 首次安装依赖

如果你的机器还没装依赖，可以运行：

```bash
cd /Users/lv/Desktop/resource_snapshot_tool
python -m pip install -r requirements.txt
playwright install chromium
```

## 使用方法

1. 打开网页。
2. 粘贴一条 `x.com/.../status/...` 或 `twitter.com/.../status/...` 链接。
3. 如果推文里有视频，可以直接填时间点，比如 `2`、`10.5`，或者 `01:23`。
4. 点击“开始截图”。
5. 图片会保存到 `screenshots/` 目录，并在网页里显示预览。

如果视频时间点留空，工具会自动尝试避开视频最开头的封面帧或黑帧。
如果勾选“追加正文中文翻译”，截图会保留原文，并在正文下方追加中文翻译；作者名、时间等信息不会翻译。

## 需要登录时

有些推文必须登录后才能访问。

这时可以：

1. 勾选网页里的“显示浏览器”。
2. 再次点击“开始截图”。
3. 在弹出的可见浏览器中完成登录。
4. 重新截图。

登录状态会保存到 `browser_profile/`，下次通常不用重新登录。
