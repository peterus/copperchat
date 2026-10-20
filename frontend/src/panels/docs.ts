import { FileText } from 'lucide-react';
import { registerPanel } from '@/layouts/panels';
import { DocsPanel } from './DocsPanel';

registerPanel({ id: 'docs', title: 'Docs', icon: FileText, component: DocsPanel, order: 12, wide: true });
