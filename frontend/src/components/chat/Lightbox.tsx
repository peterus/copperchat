import { useEffect, useState } from 'react';

export function Lightbox({ src, alt, onClose }: { src: string; alt: string; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);
  const [failed, setFailed] = useState(false);
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={alt}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/85 p-2"
      onClick={onClose}
    >
      {failed ? (
        <p className="rounded bg-black/60 px-3 py-2 text-sm text-white">The image is not available.</p>
      ) : (
        <img
          src={src}
          alt={alt}
          className="max-h-full max-w-full object-contain"
          style={{ touchAction: 'pinch-zoom' }}
          onClick={(e) => e.stopPropagation()}
          onError={() => setFailed(true)}
        />
      )}
      <button
        type="button"
        onClick={onClose}
        aria-label="Close image"
        className="absolute right-3 top-3 rounded-full bg-white/15 px-3 py-1 text-white"
      >
        ✕
      </button>
    </div>
  );
}
