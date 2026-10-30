# second proprio 桌面界面

React + TypeScript + Vite；Tauri 的本机接口连接 Python 后台。开发浏览器窗口未连接后台时会明确提示，不提供模拟业务数据。

```sh
npm ci
npm run dev
npm run test
npm run build
npm run tauri build
```

`src/api.ts` 定义受限 RPC 和返回类型。React 不直接执行 shell 或修改数据库。

- 配置显式保存并携带版本号；后端仍是 Schema 校验来源。
- 正文与笔记编辑自动保存草稿；⌘S 带基准哈希提交正文。
- 外部修改冲突展示对比，保留编辑草稿。
- AI 候选版本展示当前/候选正文及关联文件，明确采纳后发布。
- 退出界面与后台生命周期分离；停止后台先请求取消并等待当前任务结束。
- 文献 HTML 不作为可执行代码渲染，Mermaid 使用 strict 模式。

测试覆盖文件冲突、草稿恢复、候选采纳、单项重试、个人记录清空、配置版本检查和无法连接时的真实状态。测试夹具仅存在于 `src/test`。
