import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App.jsx';
import './styles.css';

// No wallet-connector framework: X Layer is reached through the injected
// EIP-1193 provider (OKX Wallet first), and auto-connect is deliberately off —
// a signing surface should never silently reconnect.
ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <BrowserRouter basename="/dashboard">
      <App />
    </BrowserRouter>
  </React.StrictMode>,
);
