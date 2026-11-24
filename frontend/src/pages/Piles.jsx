import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Piles() {
  const [list, setList] = useState([])
  const [msg, setMsg] = useState('')
  const load = () => api.桩位().then(setList)
  useEffect(() => {
    load()
  }, [])
  const setStatus = async (id, 状态) => {
    try {
      await api.改桩态(id, 状态)
      setMsg(`桩位 ${id} → ${状态}`)
    } catch (e) {
      setMsg(e.message)
    }
    load()
  }
  return (
    <div className="page">
      <h2>桩位</h2>
      <p className="lead">
        占用由「未腾空会话」派生：关账后仍显示占用未清，须拔枪确认才腾空。
      </p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>站点</th>
              <th>桩名</th>
              <th>额定功率</th>
              <th>费率</th>
              <th>展示状态</th>
              <th>占用</th>
              <th>占用未清</th>
              <th>占用会话</th>
              <th>可开充</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((p) => (
              <tr key={p.id}>
                <td>{p.site_name}</td>
                <td>{p.name}</td>
                <td>{p.rated_kw}</td>
                <td>{p.rate_name}</td>
                <td>
                  <span className={`tag ${p.展示状态}`}>{p.展示状态}</span>
                </td>
                <td>{p.占用 ? '是' : '否'}</td>
                <td>{p.占用未清 ? '是' : '否'}</td>
                <td>{p.会话号 || '—'}</td>
                <td>{p.可开充 ? '是' : '否'}</td>
                <td className="row" style={{ border: 0 }}>
                  {['空闲', '维护', '故障'].map((s) => (
                    <button
                      key={s}
                      type="button"
                      disabled={p.占用}
                      title={p.占用 ? '仍被未腾空会话占用，须拔枪确认' : ''}
                      onClick={() => setStatus(p.id, s)}
                    >
                      {s}
                    </button>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="msg">{msg}</p>
      </div>
    </div>
  )
}
