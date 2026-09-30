import React from 'react';
import { Navigate, useParams } from 'react-router-dom';

/**
 * Per-asset Earn pages are held back for the same reason the vault list is
 * (see EarnHome.jsx): no deposit or withdraw execution exists yet, so a
 * page showing a real APY next to a disabled Deposit button reads as more
 * finished than it is. Any link to a specific asset — including the ones
 * already shared — lands on the same holding notice rather than a stale
 * preview.
 */
export default function EarnAsset() {
  useParams(); // symbol is unused here on purpose; every asset redirects the same way
  return <Navigate to="/earn" replace />;
}
