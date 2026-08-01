import { PlusIcon } from './Icons.jsx'

export default function Sidebar({ open, chats, activeChatId, onNewChat, onSelectChat, onClose }) {
  return (
    <>
      <div
        className={`sidebar-scrim${open ? ' show' : ''}`}
        onClick={onClose}
        aria-hidden="true"
      />

      <aside className={`sidebar ${open ? '' : 'closed'}`}>
        <div className="sidebar-brand">
          <span className="brand-mark">AF</span>
          <span>AI for Founders</span>
        </div>

        <button className="new-chat-btn" onClick={onNewChat}>
          <PlusIcon /> New chat
        </button>

        {chats.length > 0 && <div className="sidebar-section-label">Recent</div>}

        <div className="chat-list">
          {chats.map((chat) => (
            <button
              key={chat.id}
              className={`chat-list-item ${chat.id === activeChatId ? 'active' : ''}`}
              onClick={() => onSelectChat(chat.id)}
              title={chat.title}
            >
              {chat.title}
            </button>
          ))}
        </div>

        <div className="sidebar-footer">
          Every answer is grounded in the text of <em>AI for Founders</em>, with the
          chapters it came from.
        </div>
      </aside>
    </>
  )
}
