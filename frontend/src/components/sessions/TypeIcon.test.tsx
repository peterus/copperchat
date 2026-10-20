import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { TypeIcon } from './TypeIcon';

describe('TypeIcon', () => {
  it.each([
    ['requirements', 'Requirements session'],
    ['schematic', 'Schematic session'],
    ['free', 'Free session'],
    ['board', 'Board session'],
  ] as const)('%s is labelled "%s"', (type, label) => {
    render(<TypeIcon type={type} />);
    expect(screen.getByRole('img', { name: label })).toBeInTheDocument();
  });
});
