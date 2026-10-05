# 临床实验室质量控制与结果拦截

只使用Python标准库和SQLite的模块化服务，默认端口`8339`。覆盖检测项目、质控品批次、质控规则、允许范围、仪器校准、连续偏差、趋势、失控、结果拦截、复测、调查、批次切换和历史更正。

放行结果时会冻结当时生效的**质控品批次、校准证书和规则版本**三件依据；三者中任何一样后来发生变更，已放行结果自动标记为依据失效并保留原始快照，复核人重算确认后恢复有效。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：QC规则计算、状态机、校准、放行依据快照与放行约束。
- `src/repository.py`：SQLite持久化、条件更新（版本+状态双重守卫）、幂等和审计查询。
- `src/service.py`：用例编排、权限校验、依据失效联动、断点重试和历史回填。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8339
```

## 核心对象

`assay`为检测项目，`instrument`为仪器，`qc_lot`为质控品批次，`qc_run`为质控结果，`result_batch`为患者结果批次。

## 放行依据（release basis）

执行`result_batch`的`release`时，系统校验并把以下内容冻结进`data.release_basis`：

- `qc_lot`：质控品批次ID、版本、批号、靶值、标准差；
- `calibration`：校准证书编号、仪器版本、校准有效期；
- `rules`：规则版本号和完整规则参数（rule_config）；
- 同时记录关联的质控运行（ID、版本、数值）与快照时间。

无校准证书、质控未通过或质控品批次未激活时，放行被拒绝。批次的`data.basis_status`取值：

- `valid`：依据有效；
- `invalidated`：放行后依据来源已变更，需重算；
- `pending_review`：历史数据回填时依据补不齐，待人工复核。

### 失效联动

以下变更会把相关的`released`批次置为`invalidated`，原始快照不被改写，只向`basis_history`追加失效记录，并在`basis_flags`标出原因：

| 变更动作 | 对象 | 影响范围 | 标记 |
| --- | --- | --- | --- |
| `calibrate`（新证书编号） | instrument | 该仪器放行的批次 | `calibration_certificate_changed` |
| `switch_in`（换质控品批次） | qc_lot | 使用旧批次放行的批次，旧批次自动置`retired` | `qc_lot_changed` |
| `retire` / `suspend` | qc_lot | 使用该批次放行的批次 | `qc_lot_changed` |
| `revise_rules`（规则版本+1） | assay | 该项目放行的批次 | `rule_version_changed` |

同编号校准证书的重复校准不会造成失效。重算使用`result_batch`的`revalidate`动作（可带`replacement_run_id`指向新批次的合格质控运行），通过校验后写入新快照、恢复`valid`。

### 并发放行与断点重试

- 放行提交为单事务条件更新（同时校验版本与源状态）：两名值班员同时提交同一份结果时，先提交者生效，后者收到409且不会产生第二条放行审计。
- 动作请求支持`Idempotency-Key`头：提交后落库成功但应答丢失时，用同一key重试直接返回已落库结果，不重复执行；不同key的重复提交按冲突拒绝。
- 写入遇到SQLite瞬时锁时按指数退避自动重试（默认5次），每个批次的失效标记独立提交，可从已提交的断点继续。

### 历史数据回填

`POST /api/result_batches/backfill`（可加`/<id>`只处理单条）扫描没有依据快照的已放行批次：

- 依据质控运行、仪器校准历史和规则修订记录还原当时的质控品批次、证书和规则版本，补齐后置`valid`；
- 任一来源无法还原（如缺校准证书）时置`pending_review`，`basis_flags`列出缺口（如`calibration_certificate_missing`）。

回填仅限supervisor/admin/auditor，已带快照的现代批次不会被覆盖。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤；患者结果批次还支持`?basis_status=valid|invalidated|pending_review`
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `POST /api/result_batches/backfill`、`POST /api/result_batches/<id>/backfill`
- `GET /api/audit`

身份通过`X-User-Id`和`X-Role`请求头传入。创建与动作均支持`Idempotency-Key`防止重复提交。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

质控规则为可运行的简化模型，包含1-3s、连续偏移和趋势检查，但不替代CLIA、ISO 15189、Westgard完整规则集或实验室信息系统接口。历史回填基于当前留存的校准/修订记录推断，记录已销毁时只能标待复核。
