# 真实验收记录：2026-09-08

真实 Qwen 实时链路、GPU 和浏览器基础操作已通过。当前可演示实时对话和指定记忆场景，**仍不是完整智能眼镜产品 Demo**：视觉问答质量不合格，日报/周报/月报未实现，眼镜端采集、ASR、回声与打断尚无实机验收。

## 修复内容

- 云端 caption/QA 按需加载 CLIP，Timeline API 路径不再依赖本地编码器或模型缓存。
- 语义路由关闭 thinking、输出紧凑 JSON，保留原有路由标签与超时策略。
- 备用记忆解析不再把储物柜编号、预算等数字当作电话；新的原文保存请求不继承无关电话草稿字段。
- 原文记忆支持文本中确实存在的查询约束；“我的储物柜”与“储物柜”可匹配，仍排除其他具名所有者和物品。
- 前端记忆保存回执交付后结束工作流，晚到检索事件不再错误显示持续思考。保存回执仍只有文字。
- 检查脚本必须确认 provider 连接；技能激活或部分字幕不能算回答成功。新增失败条件回归测试。
- 数据集报告区分有/无证据查询耗时，将固定“没有相关记忆”回复计入弃答。

## 真实测试结果

Windows、Python 3.13、RTX 5070 Laptop 8 GB；安装 `torch 2.8.0+cu128` 和 `torchvision 0.23.0+cu128`。官方 wheel SHA256 为 `9e20646802b7fc295c1f8b45fefcfc9fb2e4ec9cbe8593443cd2b9cc307c8405`。CUDA 矩阵乘法与 CPU 结果一致，torchvision CUDA NMS 通过，架构包含 `sm_120`；`pip check` 无冲突。这不等于本地 Qwen 模型权重已完成推理验收。

真实 API 验收通过生产 `create_app` 调用官方 `qwen3.5-omni-flash-realtime`，每次检查音频、完整字幕和持久化 `response.done`，无 mock。以下从发送完整文本 control 起算，不含麦克风采集、ASR 或扬声器播放完成时间。

| 最终普通对话样本 | 建连 | 路由 | 首文字 | 首音频 | 响应完成 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 601 ms | 1068 ms | 1448 ms | 1663 ms | 3322 ms |
| 2 | 892 ms | 1011 ms | 1331 ms | 1549 ms | 3003 ms |
| 3 | 392 ms | 1510 ms | 1851 ms | 2065 ms | 3407 ms |

三次路由均分类成功；首音频中位数 1.66 秒。修改前单次首音频 3.63 秒、路由超时 3.01 秒。样本量小且网络条件有变化，不作为生产 P95 或严格 A/B 加速比。

最终记忆场景重复两次：保存合成编号 7429 → 非空已提交卡片 ID → 查询 → 日志确认该卡片被选中 → 真实语音正确返回编号。两次均通过，查询首音频 3.61 / 3.93 秒。第二次路由两轮均超时，备用原文保存与召回仍完成；复杂记忆路由仍是瓶颈。此场景不覆盖跨进程真实模型恢复，存储重启与隔离由回归测试另行覆盖。

Edge 浏览器直接操作生产页面：注册、新建会话、退出后登录恢复通过；1440/390 像素宽度无横向溢出，无脚本异常或失败请求。截图在 `outputs/browser-final-20260908/`；没有声称此测试验证了硬件媒体。

完整 pytest 结果见 [tests.json](validation/2026-09-08/tests.json)，包含存储、隔离、路由与 API 协议回归；其中使用模拟 provider 的单元测试不计为真实 API 验收。JS 语法、Python 编译、Git diff 空白检查通过。

## EgoLife：流程通过，质量不通过

最终真实运行覆盖本地 `A1_JAKE_DAY1` 全部 **10 段视频，每段抽样 2 题，共 20 题**，不代表全量 EgoLife 或本地全部问题。采样 0.5 fps，真实 `qwen3-vl-plus`，Timeline 记忆；按视频分成校准 10 题、测试 10 题。

| 指标 | 结果 |
| --- | ---: |
| 测试 token F1 / exact match | 0.0601 / 0 |
| 测试 temporal mIoU | 0.0713 |
| 校准后覆盖率 | 0% |
| 有证据查询 | 7/20，平均 2.645 s |
| 无证据查询 | 13/20，平均 0.011 s |
| 混合平均查询时间 | 0.933 s，不能代表有效作答速度 |
| API tokens | 输入 79,096；输出 1,045；图像 77,520（输入子项） |

瓶颈：Timeline 为词法检索基线，概述丢失细节，许多问题没有共享词而无法召回；输入只读取画面，“What specific instruction was given”等问题需要音轨；时间定位输出整段边界，粒度过粗。弃答句和参考答案的通用词重合仍可能产生少量非零 F1，不意味着回答正确。没有注入标准答案或放宽数据隔离来提高指标。

## 常驻运行与复现

常驻 Demo：`http://127.0.0.1:8780`，独立数据目录 `.egoassist-real-demo/`。已从该实例收到真实 Qwen 音频和完整回答，只绑定本机；用户可在页面注册。日志：`outputs/demo-8780.*.log` 和 `.egoassist/server.log`。重启命令：

```powershell
.\.venv\Scripts\python.exe run_egoassist.py --port 8780 --memory-path .egoassist-real-demo/runtime.json
```

复测须使用新的输出目录；浏览器脚本需 Python `playwright` 包和已安装的 Edge，API 脚本读取项目 `.env` 的 `QWEN_API_KEY`。

```powershell
.\.venv\Scripts\python.exe scripts/verify_cuda.py --output outputs/cuda-new.json
.\.venv\Scripts\python.exe scripts/verify_real_session.py --output outputs/realtime-new
.\.venv\Scripts\python.exe scripts/verify_real_session.py --scenario memory --output outputs/memory-new
.\.venv\Scripts\python.exe scripts/verify_browser.py --output outputs/browser-new
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe run_egolife_benchmark.py --data-dir data_real/egolife/A1_JAKE_DAY1 --output-dir outputs/dataset-new --backend dashscope --api-model qwen3-vl-plus --max-questions-per-video 2 --fps 0.5
```

API Timeline 路径无须下载 CLIP/Qwen 本地权重，视频解码需 OpenCV。安装项目基础依赖后，PyTorch 应单独按平台选择；本机实测版本见 [cuda.json](validation/2026-09-08/cuda.json)。

此前 `WinError 10013` 出现在受限执行环境；获准联网后官方 API 正常。不能据此判定用户防火墙规则无效，`EnforcementStatus: NotApplicable` 也不足以支持该结论。本次未再修改防火墙、DNS 或代理。

精简结果、数据集视频/QA 哈希和原始日志 SHA256 存于 [validation](validation/2026-09-08/provenance.json)。不提交密钥、账号数据库、原始视频、模型文件或完整用户日志。
