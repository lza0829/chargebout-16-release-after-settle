import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Settle() {
  const [list, setList] = useState([])
  const [pending, setPending] = useState([])
  const [msg, setMsg] = useState('')
  const load = () =>
    Promise.all([
      Promise.all([api.会话('充电中'), api.会话('已超时'), api.会话('结算中')]),
      api.待拔枪(),
    ]).then(([[a, b, c], p]) => {
      setList([...a, ...b, ...c])
      setPending(p)
    })
  useEffect(() => {
    load()
  }, [])
  const go = async (id) => {
    setMsg('')
    try {
      const r = await api.结算(id)
      setMsg(`已关账：电量 ${r.电量}，金额 ${r.金额}；桩枪仍占用，待拔枪确认腾空`)
    } catch (e) {
      setMsg(e.message)
    }
    load()
  }
  const unplug = async (id) => {
    setMsg('')
    try {
      const r = await api.拔枪确认(id, '结算后拔枪')
      setMsg(`会话 ${r.会话编号} 已拔枪确认，桩枪腾空于 ${r.腾空时间}`)
    } catch (e) {
      setMsg(e.message)
    }
    load()
  }
  return (
    <div className="page">
      <h2>结算关账</h2>
      <p className="lead">
        关账只落金额与已关账；桩位/枪线保持占用，等拔枪确认后置腾空。关账与腾空同事务、可回滚。
      </p>

      <div className="panel">
        <h3>待关账</h3>
        <table>
          <thead>
            <tr>
              <th>会话号</th>
              <th>桩位</th>
              <th>状态</th>
              <th>电量</th>
              <th>预估金额</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((s) => (
              <tr key={s.id}>
                <td>{s.slip_no}</td>
                <td>
                  {s.site_name} · {s.pile_name}
                </td>
                <td>
                  <span className={`tag ${s.status}`}>{s.status}</span>
                </td>
                <td>{s.kwh}</td>
                <td>{s.amount}</td>
                <td>
                  <button type="button" className="primary" onClick={() => go(s.id)}>
                    结算关账
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h3>待拔枪确认（已关账，占用未清）</h3>
        <table>
          <thead>
            <tr>
              <th>会话号</th>
              <th>桩位 · 枪线</th>
              <th>会话状态</th>
              <th>金额</th>
              <th>关账时间</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {pending.map((s) => (
              <tr key={s.id}>
                <td>{s.slip_no}</td>
                <td>
                  {s.pile_name} · {s.gun_name}
                </td>
                <td>
                  <span className={`tag ${s.status}`}>{s.status}</span>
                </td>
                <td>{s.amount}</td>
                <td>{s.closed_at}</td>
                <td>
                  <button type="button" className="primary" onClick={() => unplug(s.id)}>
                    拔枪确认腾空
                  </button>
                </td>
              </tr>
            ))}
            {pending.length === 0 && (
              <tr>
                <td colSpan={6}>无待腾空会话</td>
              </tr>
            )}
          </tbody>
        </table>
        <p className={`msg ${msg.startsWith('已关账') || msg.includes('腾空') ? 'ok' : ''}`}>{msg}</p>
      </div>
    </div>
  )
}
