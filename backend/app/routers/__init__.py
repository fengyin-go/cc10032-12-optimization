"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import incident, resource

ROUTERS = [resource, incident]
# 说明：incident 路由含固定前缀 /api/incidents 与 /api/migration，
# 必须在通用 /api/{module}/{entry_id} 之前注册，否则会被动态路径抢匹配。
REGISTER_ORDER = [incident, resource]
