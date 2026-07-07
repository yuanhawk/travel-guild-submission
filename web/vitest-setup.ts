// Global vitest setup. Registers jest-dom matchers (toBeVisible, toHaveClass, …) for the
// component tests. Harmless for the pure-node unit tests (it only extends `expect`).
import '@testing-library/jest-dom/vitest';
