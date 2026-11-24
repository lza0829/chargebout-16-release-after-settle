import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Guns() {
  const [list, setList] = useState([])
  useEffect(() => {
    api.枪线().then(setList)
  }, [])
  return (
    <div className="page">
      <h2>枪线</h2>
      <p className="lead">
        枪线占用与桩位同源同命运：同一笔会话关账后仍占枪，拔枪确认才归还。
      </p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>桩位</th>
              <th>枪名</th>
              <th>展示状态</th>
              <th>占用</th>
              <th>占用未清</th>
              <th>占用会话</th>
              <th>可开充</th>
            </tr>
          </thead>
          <tbody>
            {list.map((g) => (
              <tr key={g.id}>
                <td>{g.pile_name}</td>
                <td>{g.name}</td>
                <td>
                  <span className={`tag ${g.展示状态}`}>{g.展示状态}</span>
                </td>
                <td>{g.占用 ? '是' : '否'}</td>
                <td>{g.占用未清 ? '是' : '否'}</td>
                <td>{g.会话号 || '—'}</td>
                <td>{g.可开充 ? '是' : '否'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
