# 实时计算服务中心

面向实时数据分析场景，提供流式计算定义、窗口聚合、策略告警和状态管理的实时数据平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/views/AlertMergeView.vue     告警合并清单（12号）
│   ├── src/views/RestoreCycleView.vue   恢复方案与周期（12号）
│   ├── src/views/DeployMigrationView.vue 迁移与部署（12号）
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/routers/incident.py  告警合并/恢复周期/迁移切换接口（12号）
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/incident_ledger.py 同源合并、周期、回执台账（12号）
│   ├── app/services/migration_switch.py 切换路径、幂等与事务回滚（12号）
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 流式源端 | `stream_source` | 数据源配置 | 源端名称、消息队列、Topic名称 |
| 窗口配置 | `window_config` | 窗口策略 | 策略名称、窗口类型、窗口大小 |
| 计算算子 | `calc_operator` | 算子定义 | 算子名称、算子类型、输入字段 |
| 状态存储 | `state_store` | 状态配置 | 配置名称、后端类型、状态键模式 |
| 水位追踪 | `watermark_track` | 水位记录 | 水位名称、数据源、当前水位 |
| 告警策略 | `alert_policy` | 告警规则 | 规则名称、监控指标、触发条件 |
| 滑动指标 | `sliding_index` | 指标计算 | 指标名称、计算窗口、聚合函数 |
| 流关联 | `join_stream` | 关联配置 | 配置名称、左流源、右流源 |
| 结果落地 | `sink_output` | 输出配置 | 配置名称、输出类型、目标地址 |
| 补数任务 | `backfill_job` | 补数作业 | 作业名称、目标指标、补数范围 |
| CEP规则 | `cep_rule` | 事件序列 | 序列名称、事件类型、匹配顺序 |
| 吞吐管理 | `throughput_mgr` | 吞吐配置 | 配置名称、数据源、最大QPS |
| 检查点 | `checkpoint_mgr` | 检查点配置 | 配置名称、间隔时间、超时时长 |
| 故障恢复 | `restore_plan` | 恢复方案 | 方案名称、任务范围、恢复模式 |
| 指标血缘 | `metric_lineage` | 血缘记录 | 记录ID、指标名称、上游源端 |
| 迟到处理 | `late_handler` | 处理策略 | 策略名称、允许延迟、迟到动作 |
| 任务拓扑 | `task_topology` | 拓扑配置 | 拓扑名称、算子链、并行度 |
| 数据采样 | `data_sampler` | 采样策略 | 策略名称、采样方式、采样比例 |
| 调试追踪 | `debug_trace` | 调试会话 | 会话名称、目标算子、采样数据 |
| 聚合落库 | `aggregate_sink` | 聚合任务 | 任务名称、源指标集、聚合粒度 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 12号：同源告警合并与迁移切换

告警升级不再为每次 CEP 命中新建恢复方案。一次命中是一条不可丢失的触发记录，
同源（CEP 规则 + 任务范围）的连续命中共用一份合并清单和一个当前恢复方案。

页面（左侧导航前三项）：

| 页面 | 路径 | 作用 |
| --- | --- | --- |
| 告警合并清单 | `/alert/merge` | 查看 CEP 命中数据、合并数量与触发证据，可模拟连续命中 |
| 恢复方案与周期 | `/restore/cycles` | 确认每个活动方案对应的周期、触发证据、回执版本与构建流水线 |
| 迁移与部署 | `/deploy/migration` | 两条切换路径取舍、执行切换、迁移台账与迁移后核对 |

业务约束：

- 同源告警只对应一个当前恢复方案；同一任务范围最终只有一个活动恢复范围。
- 合并后保留全部触发记录（`_hit_ids` / 合并清单的 `hit_ids`），关闭的周期同样保留。
- 恢复完成后同一规则再次连续触发有两种处理：`extend` 合并延长当前方案，
  `auto`/`new_cycle` 关闭上一周期后开启新方案；页面始终只有一个活动恢复方案。
- 恢复方案回执按 `source_version` 留存：迁移后保留 `legacy-v1` 回执并追加
  `v2-migrated` 回执，不覆盖旧版本。
- 环境切换以 `switch_key` 幂等，重复提交只生效一次（并发下同样只提交一次）；
  事务失步时快照回滚原版本，失败不占用幂等键，可修复后重试。
- 两条切换路径：`atomic` 整批一致回滚，`rolling` 逐方案迁移补偿回滚。

关键接口：

```text
GET  /api/incidents/merge-manifests          告警合并清单与合并数量
GET  /api/incidents/cep-hits                 逐条 CEP 命中告警数据
POST /api/incidents/cep-hits                 写入命中并同源合并
GET  /api/incidents/restore-plans            方案 → 周期 → 证据 → 回执
POST /api/incidents/restore-plans/{id}/complete
GET  /api/incidents/receipts                 按来源版本留存回执
GET  /api/migration/status                   迁移前后结果与台账
GET  /api/migration/paths                    两种切换路径取舍
GET  /api/migration/build-pipeline           部署页/恢复详情构建流水线
GET  /api/migration/trace                    顺 CEP 调用链定位旧环境数据
POST /api/migration/switch                   执行切换（幂等 + 事务回滚）
```

领域：数据工程
子方向：数据分析与数据应用
