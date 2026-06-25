import React from 'react';
import ReactDOM from 'react-dom/client';

import { App } from './App';
import './styles.css';

// 前端入口保持单一职责：挂载 React 应用，业务状态放在 App 内部管理。
ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
