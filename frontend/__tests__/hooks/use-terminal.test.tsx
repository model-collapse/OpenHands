import { act } from "@testing-library/react";
import { beforeAll, describe, expect, it, vi, afterEach } from "vitest";
import { useTerminal } from "#/hooks/use-terminal";
import { Command, useCommandStore } from "#/stores/command-store";
import { renderWithProviders } from "../../test-utils";

// Mock the WsClient context
vi.mock("#/context/ws-client-provider", () => ({
  useWsClient: () => ({
    send: vi.fn(),
    status: "CONNECTED",
    isLoadingMessages: false,
    events: [],
  }),
}));

// Mock useActiveConversation
vi.mock("#/hooks/query/use-active-conversation", () => ({
  useActiveConversation: () => ({
    data: {
      id: "test-conversation-id",
      conversation_version: "V0",
    },
    isFetched: true,
  }),
}));

// Mock useConversationWebSocket (returns null for V0 conversations)
vi.mock("#/contexts/conversation-websocket-context", () => ({
  useConversationWebSocket: () => null,
}));

function TestTerminalComponent() {
  const ref = useTerminal();
  return <div ref={ref} />;
}

describe("useTerminal", () => {
  // Terminal is read-only - no longer tests user input functionality
  const mockTerminal = vi.hoisted(() => ({
    loadAddon: vi.fn(),
    open: vi.fn(),
    write: vi.fn(),
    writeln: vi.fn(),
    clear: vi.fn(),
    dispose: vi.fn(),
    element: document.createElement("div"),
  }));

  const mockFitAddon = vi.hoisted(() => ({
    fit: vi.fn(),
  }));

  beforeAll(() => {
    // mock ResizeObserver - use class for Vitest 4 constructor support
    window.ResizeObserver = class {
      observe = vi.fn();

      unobserve = vi.fn();

      disconnect = vi.fn();
    } as unknown as typeof ResizeObserver;

    // mock Terminal - use class for Vitest 4 constructor support
    vi.mock("@xterm/xterm", async (importOriginal) => ({
      ...(await importOriginal<typeof import("@xterm/xterm")>()),
      Terminal: class {
        loadAddon = mockTerminal.loadAddon;

        open = mockTerminal.open;

        write = mockTerminal.write;

        writeln = mockTerminal.writeln;

        clear = mockTerminal.clear;

        dispose = mockTerminal.dispose;

        element = mockTerminal.element;
      },
    }));

    // mock FitAddon
    vi.mock("@xterm/addon-fit", () => ({
      FitAddon: class {
        fit = mockFitAddon.fit;
      },
    }));
  });

  afterEach(() => {
    vi.clearAllMocks();
    // Reset command store between tests
    useCommandStore.setState({ commands: [] });
  });

  it("should render", () => {
    renderWithProviders(<TestTerminalComponent />);
  });

  it("should render the commands in the terminal", () => {
    const commands: Command[] = [
      { content: "echo hello", type: "input" },
      { content: "hello", type: "output" },
    ];

    // Set commands in store before rendering to ensure they're picked up during initialization
    useCommandStore.setState({ commands });

    renderWithProviders(<TestTerminalComponent />);

    expect(mockTerminal.writeln).toHaveBeenNthCalledWith(1, "echo hello");
    expect(mockTerminal.writeln).toHaveBeenNthCalledWith(2, "hello");
  });

  it("should not call fit() when terminal.element is null", () => {
    // Temporarily set element to null to simulate terminal not being opened
    const originalElement = mockTerminal.element;
    mockTerminal.element = null as unknown as HTMLDivElement;

    renderWithProviders(<TestTerminalComponent />);

    // fit() should not be called because terminal.element is null
    expect(mockFitAddon.fit).not.toHaveBeenCalled();

    // Restore original element
    mockTerminal.element = originalElement;
  });

  it("renders new commands after the command buffer is cleared", () => {
    // Start with some commands already rendered.
    useCommandStore.setState({
      commands: [
        { content: "echo hello", type: "input" },
        { content: "hello", type: "output" },
      ] satisfies Command[],
    });

    renderWithProviders(<TestTerminalComponent />);
    expect(mockTerminal.writeln).toHaveBeenCalledWith("hello");

    // Clearing the terminal empties the shared command buffer (this happens
    // e.g. when switching conversations).
    act(() => {
      useCommandStore.getState().clearTerminal();
    });

    // A new command for the fresh buffer must still render. Previously a stale
    // write cursor (left pointing past the end of the now-shorter buffer) caused
    // these to be silently dropped.
    act(() => {
      useCommandStore.getState().appendOutput("world");
    });

    expect(mockTerminal.writeln).toHaveBeenCalledWith("world");
  });
});
