import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App.jsx';
import { WalletProvider } from './privy.jsx';
import './styles.css';

// No wallet-connector framework: X Layer is reached through the injected
// EIP-1193 provider (OKX Wallet first), and auto-connect is deliberately off —
// a signing surface should never silently reconnect.
//
// VITE_ROUTER_BASE lets a build be served from a sub-path with its internal
// links staying inside it. Only the frozen /legacy snapshot sets it; the
// normal build serves from the root and leaves it unset.
ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <BrowserRouter basename={import.meta.env.VITE_ROUTER_BASE || '/'}>
      <WalletProvider>
        <App />
      </WalletProvider>
    </BrowserRouter>
  </React.StrictMode>,
);
