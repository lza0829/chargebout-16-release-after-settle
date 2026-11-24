# 充桩条

开会话 → 计量脉冲累加 → 结算关账 → 拔枪确认腾空 → 超时/异常命运。

- 前端 http://127.0.0.1:6500
- API http://127.0.0.1:11500

```powershell
# 本地 API
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 11500

# 本地前端（另开）
cd frontend
npm install
npm run dev
```

或 `docker compose up -d --build`。

## 结算关账后的腾空时序

全局只贯彻一种口径：**以拔枪确认后置腾空为准**。

1. `POST /api/会话/{id}/结算` 在同一事务里落「已关账 + 金额 + 关账事件 + 关账占用事件」；
   桩位与枪线**保持占用**，读路径标记为「占用未清 / 待拔枪确认」。
2. 未腾空前，`POST /api/开会话` 对该桩/枪返回 409，不会出现「未腾空又开充成功」。
3. `POST /api/会话/{id}/拔枪确认` 是唯一腾空写口：同一事务写 `released_at` 与腾空事件，
   桩位页、枪线页、会话详情、总览空闲数同刻派生为「空闲 / 可用」。
4. 异常中断同属关账性质（只终止，不结算残额），腾空同样等拔枪确认。

占用不是桩位/枪线上的布尔字段，而是从会话时序（`occupancy_events` 与
`sessions.released_at`）派生；`app/services/occupancy.py` 是唯一读模型。
`GET /api/会话/一致性` 会把「已关仍占用、双占、手置占用、占用与重放不符」逐条对照，
并给出按时序重放的当前占用，供跨页对读。

### 夹具

```powershell
cd backend
.\.venv\Scripts\pip install pytest httpx
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\python -m pytest tests -q
```

覆盖：关账后仍占枪、拔枪前二次开充、释放与关账半截回滚、空闲态与会话已关拧花、
枪线页已归还桩位页仍占用、总览空闲数与占用列对不上、并发双开与并发腾空/再开充。
