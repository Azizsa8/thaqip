import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { SearchPage } from './pages/SearchPage';
import { AskPage } from './pages/AskPage';
import { UnavailablePage } from './pages/UnavailablePage';
import { DocumentDetailPage } from './pages/DocumentDetailPage';

import './index.css';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 1000 * 60 * 5,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/dashboard" element={<UnavailablePage />} />
          <Route path="/" element={<SearchPage />} />
          <Route path="/ask" element={<AskPage />} />
          <Route path="/browse" element={<UnavailablePage />} />
          <Route path="/source/:sourceId" element={<UnavailablePage />} />
          <Route path="/source/:sourceId/article/:articleNo" element={<DocumentDetailPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}

export default App;