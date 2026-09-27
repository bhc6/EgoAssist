# 后台观察：每次符合条件都提醒

默认每次新的已接受观察，只要当前画面仍满足有效提醒目标，就允许再次播报。同一目标、相同措辞、相同可见内容以及历史播报记录不再阻止新观察的提醒；重启后仍遵循此策略。画面中的触发条件必须成立，不是每一帧都无条件说话。

后端取消跨观察事项指纹的 `already-offered` 和 `already-queued` 门控；事项指纹与历史播报时间仅保留用于审计关联。视觉提示词同步取消“已播过就保持沉默”。`questionCooldownSeconds` 仅限制主动询问，不限制提醒，界面同步标注。

同一个 observation ID 重复投递不会重新生成 review 或再次调度语音，音频发送回执也保持幂等。观察授权、提醒开关、有效目标、证据校验、画面时效和输出权限仍生效；用户正在对话或还有输出时，观察不会强行插话。被阻止的旧观察不会在解禁后重放，而由新采样重新判断。

历史记录中的 `already-offered` 保留为旧策略审计，不改写过去的实际结果。前端将其注明为旧策略。

回归覆盖：连续采样重复提醒、相同画面内容、重启后的新提醒、相同 observation ID 幂等、交互消费者只调度一次、历史已播报后的权限/忙碌/过期门控，以及保留主动询问间隔。

验证：`test_observation_reminders.py`、`test_observation_reviews.py`、`test_ambient_perception.py`、`test_observation_lanes.py`、`test_ambient_console.py`、`test_realtime_api.py` 共 125 项通过。两个修改后的前端脚本通过 `node --check`，`git diff --check` 通过。8770 服务返回 HTTP 200，所服务的 `ambient.js` 与 `app.js` 和本地文件逐字节一致。本轮未进行物理摄像头、扬声器或真实视觉模型重复提醒实测。
