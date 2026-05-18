# 每日查询次数限制 — 调整指南

## 优先级

```
单用户 max_requests（DB） > 角色默认 DAILY_LIMITS > 兜底值 25
```

## 调整方式

### 1. 全局调整（按角色）

编辑 `hermes_state.py:2301`：

```python
DAILY_LIMITS = {"free": 100, "vip": 500}
```

改完**同步检查** `api_server.py:1276` 的 `UPGRADE_MSG` 提示文案和前端 `chat.vue` 的兜底文案。

重启：`systemctl restart hermes-gateway`

### 2. 单用户覆盖

```sql
sqlite3 /root/.hermes/state.db \
  "UPDATE user_quotas SET max_requests = 200 WHERE user_id = '要改的用户openid'"
```

`max_requests = 0` 表示不覆盖，走角色默认值。即时生效，无需重启。

### 3. 用户升 VIP（角色切换）

```sql
sqlite3 /root/.hermes/state.db \
  "UPDATE user_quotas SET user_role = 'vip' WHERE user_id = '要改的用户openid'"
```

### 4. 批量设置（管理员接口）

```
POST /v1/admin/quota
Header: X-Api-Key: HDLMgsDz9VdPlnNr
Body: {"user_id": "xxx", "max_requests": 200, "mode": "set"}
```

## 验证

```bash
# 查看用户的当前限额和已用量
curl -s -H "X-User-Id: <openid>" http://localhost:8642/v1/user/daily-status | python3 -m json.tool

# 返回示例
# {"daily_limit": 25, "daily_used": 5, "daily_remaining": 20}
```

## 关键文件

| 文件 | 作用 |
|---|---|
| `hermes_state.py:2301` | `DAILY_LIMITS` 角色默认值 |
| `hermes_state.py:2303` | `resolve_daily_limit()` 优先级解析 |
| `api_server.py:1276` | `UPGRADE_MSG` 超限提示文案 |
| `api_server.py:1278` | `_enforce_daily_limit()` 请求拦截 |
| `api_server.py:1329` | `_handle_daily_status()` 前端查询接口 |
| `/opt/stock/src/pages/chat/chat.vue:38` | 前端弹窗兜底文案 |
