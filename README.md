# Bilibili 会员购抢票助手

> ⚠️ **声明：本工具仅供个人学习与研究使用，严禁用于任何形式的商业牟利或黄牛倒卖行为。**

一个基于 Python + Flask 的 Bilibili 会员购（show.bilibili.com）自动化抢票工具，提供现代化的 Web 可视化界面。

---

## 功能特性

- **扫码/Cookie 双登录** — 支持 B 站 App 扫码登录或手动粘贴 Cookie
- **商品智能查询** — 输入商品 ID 自动列出 SKU 票档和场次，交互式选择
- **后台监控抢票** — 自适应频率轮询库存，检测到有票全自动秒杀下单
- **实时状态仪表盘** — 轮询次数、库存状态、运行时间一目了然
- **手动下单测试** — 不依赖监控，随时手动触发下单
- **本地 KV 缓存** — 内存优先读写，减少高频循环中的磁盘 IO
- **令牌桶限速** — 内置请求频率控制，避免触发 412 风控封禁
- **桌面级体验** — 一键启动，自动打开浏览器，深色主题现代 UI

---

## 界面预览

启动后自动打开浏览器，深色主题单页应用：

- **左侧**：账号登录 + 抢票目标配置
- **右侧**：监控控制面板 + 实时运行日志

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 启动应用

```bash
python start_gui.py
```

或双击 `启动抢票助手.bat`（可发送到桌面快捷方式）。

浏览器将自动打开 `http://127.0.0.1:5199`。

### 3. 使用流程

1. **登录** — 扫码或粘贴 Cookie
2. **设置目标** — 输入商品页 URL 中的 `id` 参数（如 `https://show.bilibili.com/platform/detail.html?id=75736` 中的 `75736`），点击查询后选择 SKU 和场次
3. **启动监控** — 点击「启动监控」，工具开始后台轮询库存
4. **自动抢票** — 检测到有票时自动下单，弹窗提示订单号，去 B 站手动付款即可

---

## 配置说明

编辑 `config.yaml` 可调整以下参数：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `polling.interval_ms` | 800 | 有票时的轮询间隔（毫秒） |
| `polling.pre_sale_refresh_ms` | 3000 | 未开售/售罄时的慢速间隔 |
| `rate_limit.max_rps` | 1.5 | 每秒最大请求数 |
| `rate_limit.cooldown_seconds` | 30 | 触发 412 风控后冷却时间 |
| `ticket.count` | 1 | 购买数量 |

---

## 打包为 EXE

```bash
python build_exe.py
```

生成 `dist/抢票助手.exe`，无依赖单文件，可复制到任意 Windows 电脑运行。

---

## 项目结构

```
抢票助手/
├── start_gui.py           # 桌面启动入口
├── 启动抢票助手.bat        # 双击启动批处理
├── config.yaml            # 配置文件
├── build_exe.py           # PyInstaller 打包脚本
├── src/
│   ├── app.py             # Flask 后端 API 服务
│   ├── web/index.html     # 前端界面 (深色主题)
│   ├── bilibili_api.py    # B 站 API 客户端 + 令牌桶限速
│   ├── monitor.py         # 票务状态监控模块
│   ├── order.py           # 下单执行器
│   ├── config.py          # 配置管理
│   └── kv_cache.py        # 本地 KV 缓存
└── data/                  # 缓存 & 日志目录
```

---

## 技术栈

- **后端**: Python 3.13 + Flask
- **前端**: 原生 HTML/CSS/JS（无框架依赖）
- **限速**: 令牌桶算法
- **缓存**: 内存 dict + JSON 文件持久化
- **打包**: PyInstaller

---

## 注意事项

1. **遵守平台规则** — 合理设置请求频率，避免对服务器造成负担
2. **412 风控** — 过度频繁请求会触发 `412 Precondition Failed`，导致 IP 临时封禁
3. **Cookie 有效期** — SESSDATA 过期后需重新登录
4. **仅限个人使用** — 不得用于商业牟利或黄牛倒卖

---

## License

MIT — 仅供学习研究使用
