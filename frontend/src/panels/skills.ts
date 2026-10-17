import { BookOpen } from 'lucide-react';
import { registerPanel } from '@/layouts/panels';
import { SkillsPanel } from '@/pages/skills/SkillsPanel';

registerPanel({ id: 'skills', title: 'Skills', icon: BookOpen, component: SkillsPanel });
