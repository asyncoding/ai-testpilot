"""问答工作流图（docs/08 FR-QA-001～004，S1.4）。

包内纪律：
- state.py 的 QaState 只流通 JSON 原语（checkpoint 序列化限制，见
  tests/test_langgraph_smoke.py 的取证性测试）；
- nodes.py 的节点函数与 LangGraph 无关（async f(state) -> dict），框架只在
  graph.py 薄装配——若未来换手写状态机，节点零改动；
- 节点无业务落库副作用：qa_runs 由 QaService 在图执行后统一写一行。
"""
