import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { SuiClientProvider, WalletProvider } from '@mysten/dapp-kit';
import '@mysten/dapp-kit/dist/index.css';
import App from './App.jsx';
import './styles.css';

// Wallet connections cover zkLogin wallets too (Slush signs via popup-based
// zkLogin — Google/Apple/Twitch — same pattern as this dev's other projects).
// autoConnect is off on purpose: a signing surface should not silently
// reconnect; the user re-establishes the session explicitly.
const queryClient = new QueryClient();
const networks = { mainnet: { url: 'https://fullnode.mainnet.sui.io:443' } };

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <SuiClientProvider networks={networks} defaultNetwork="mainnet">
        <WalletProvider autoConnect={false}>
          <BrowserRouter basename="/dashboard">
            <App />
          </BrowserRouter>
        </WalletProvider>
      </SuiClientProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
