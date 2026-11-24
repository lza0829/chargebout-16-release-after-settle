import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'

const STATS = [
  ['桩位空闲', '桩位空闲'],
  ['桩位占用', '桩位占用'],
  ['占用未清', '桩位占用未清'],
  ['可开充', '桩位可开充'],
  ['桩位故障', '桩位故障'],
  ['桩位维护', '桩位维护'],
  ['枪线可用', '枪线可用'],
  ['枪线占用', '枪线占用'],
  ['枪线占用未清', '枪线占用未清'],
  ['充电中', '充电中'],
  ['结算中', '结算中'],
  ['已关账', '已关账'],
  ['已超时', '已超时'],
  ['已中断', '已中断'],
  ['待拔枪确认', '待拔枪确认'],
  ['在充电量', '在充电量'],
  ['本日关账金额', '本日关账金额'],
]

export default function Home() {
  const [o, setO] = useState(null)
  const [check, setCheck] = useState(null)
  const [err, setErr] = useState('')
  const load = useCallback(() => {
    Promise.all([api.总览(), api.一致性()])
      .then(([ov, ck]) => {
        setO(ov)
        setCheck(ck)
        setErr('')
      })
      .catch((e) => setErr(e.message))
  }, [])
  useEffect(() => {
    load()
  }, [load])
  if (!o) return <div className="page"><p className="msg">{err || '加载中'}</p></div>
  return (
    <div className="page">
      <h2>总览</h2>
      <p className="lead">
        营业日 {o.营业日} · 充日{o.充日已封印 ? '已封印' : '未封印'} · 占用口径：{o.口径}
      </p>
      <div className="grid">
        {STATS.map(([k, key]) => (
          <div className="stat" key={key}>
            <div className="k">{k}</div>
            <div className="v">{o[key]}</div>
          </div>
        ))}
      </div>
      <div className="panel">
        <h3>占用时序自检</h3>
        <p className={check?.一致 ? 'msg ok' : 'msg'}>
          {check ? (check.一致 ? '占用与腾空时序重放一致' : `发现 ${check.异常.length} 项不一致`) : '检查中'}
        </p>
        {check && !check.一致 && (
          <table>
            <thead>
              <tr>
                <th>类型</th>
                <th>对象</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              {check.异常.map((a, i) => (
                <tr key={i}>
                  <td>{a.类型}</td>
                  <td>
                    {a.会话编号 ?? a.桩位编号 ?? a.枪线编号 ?? a.会话?.join(' / ') ?? ''}
                  </td>
                  <td>{a.说明 || JSON.stringify(a)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <button type="button" onClick={load}>刷新</button>
      <p className="msg">{err}</p>
    </div>
  )
}
