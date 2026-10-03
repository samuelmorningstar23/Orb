import { useState, useEffect } from 'react';
import './RelayComponents.css';

interface RelaySearchProps {
  onSearch: (query: string) => void;
}

export default function RelaySearch({ onSearch }: RelaySearchProps) {
  const [query, setQuery] = useState('');

  // Debounce the search input slightly to prevent jumpiness
  useEffect(() => {
    const timer = setTimeout(() => {
      onSearch(query);
    }, 150);
    return () => clearTimeout(timer);
  }, [query, onSearch]);

  return (
    <div className="relay-search-container">
      <div className="relay-search-input-wrapper glass">
        <svg className="relay-search-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <circle cx="11" cy="11" r="8"></circle>
          <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
        </svg>
        <input 
          type="text" 
          className="relay-search-input" 
          aria-label="Search case rooms"
          placeholder="Search patient, ID, or case label..." 
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        {query && (
          <button type="button" className="relay-search-clear" onClick={() => setQuery('')} aria-label="Clear search">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
          </button>
        )}
      </div>
    </div>
  );
}
