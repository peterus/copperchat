import { Cpu } from 'lucide-react';
import { registerPanel } from '@/layouts/panels';
import { PartsPanel } from '@/pages/parts/PartsPanel';

registerPanel({ id: 'parts', title: 'Parts', icon: Cpu, component: PartsPanel });
