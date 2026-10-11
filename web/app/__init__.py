"""web.app — 应用层 VM 编排（业务层子模块）。

按 README「应用层机制」节：本目录是**渐进迁移**目标，把
``web/routers/*.py`` 内联的 VM 编排（``daily-image`` / ``gap`` / ``movement``
等）抽出来做"每屏一个编排函数"。当前只有 ``web.app.jc.daily_image`` 作为
样板，**router 退化为纯透传**：

    # Before — VM 编排 + 路由合一
    @router.get("/daily-image")
    def daily_image(...):
        rows = ...
        return {"available": True, "rows": rows, ...}  # VM

    # After — 路由透传，VM 在 web.app.jc
    @router.get("/daily-image")
    def daily_image(...):
        return app.jc.daily_image_vm(sport="football")

迁移路径（README 已记录）：
  1. 在 ``web/app/<screen>.py`` 写纯函数 ``def <screen>_vm(...) -> dict``，无
     FastAPI 依赖、可单测；
  2. router 端点退化为 ``return app.<screen>.daily_image_vm(...)``；
  3. URL 路径保持 ``/api/jc/*``（前端无感）；
  4. 路由签名不变（``require_auth``、参数类型、降级语义不变）。

新增端点请遵循此模式；``web.app`` 仅放纯函数 VM，**不**含：
  - ``Depends(require_auth)``（鉴权只在 router 层）
  - ``raise HTTPException(...)``（降级语义用 ``return {available: False, reason: ...}``）
  - 任何 ``from ai.*`` 顶层 import（合约 §5；走 ``web.services.news`` 网关）
"""

__all__: list[str] = []
