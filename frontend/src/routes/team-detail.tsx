/* eslint-disable i18next/no-literal-string */
// Internal operator UI (AI Teams). Literal strings are acceptable here, matching
// the precedent in admin-dashboard.tsx / plan-preview.tsx; not user-i18n scope.
import React from "react";
import { Link, useNavigate, useParams } from "react-router";
import {
  useTeamAgents,
  useTeamBoard,
  useTeamLeadConversation,
} from "#/hooks/query/use-teams";

/**
 * Team detail (design §15): board (issues grouped by state) + roster + a link
 * that opens the grand-leader<->lead conversation in the standard conversation
 * UI. Read-mostly — the human steers by talking to the lead, not by editing
 * state here.
 */
function TeamDetailScreen() {
  const { teamId } = useParams();
  const navigate = useNavigate();
  const { data: board, isLoading } = useTeamBoard(teamId, "all");
  const { data: agents } = useTeamAgents(teamId);
  const { data: leadConversationId, refetch: refetchLeadConvo } =
    useTeamLeadConversation(teamId);
  const [opening, setOpening] = React.useState(false);

  const openLeadChat = async () => {
    if (leadConversationId) {
      navigate(`/conversations/${leadConversationId}`);
      return;
    }
    // Not opened yet — the endpoint lazily starts it (sandbox needs a few
    // seconds to reach READY). Refetch until we get a real conversation id.
    setOpening(true);
    try {
      const { data } = await refetchLeadConvo();
      if (data) {
        navigate(`/conversations/${data}`);
      }
    } finally {
      setOpening(false);
    }
  };

  return (
    <div
      data-testid="team-detail-screen"
      className="px-6 py-8 h-full overflow-y-auto custom-scrollbar-always max-w-[1000px] mx-auto w-full"
    >
      <div className="flex items-center gap-3 mb-4">
        <Link to="/teams" className="text-sm text-neutral-400 hover:underline">
          ← Teams
        </Link>
        <h1 className="text-xl font-semibold grow">{teamId}</h1>
        <button
          type="button"
          onClick={openLeadChat}
          disabled={opening}
          data-testid="open-lead-conversation"
          className="bg-primary text-black rounded-md px-4 py-2 text-sm font-medium disabled:opacity-50"
          title="Talk to the team lead"
        >
          {opening ? "Opening…" : "Open lead conversation"}
        </button>
      </div>

      <div className="grid md:grid-cols-[2fr_1fr] gap-6">
        {/* Board */}
        <div>
          <h2 className="text-sm font-medium text-neutral-300 mb-2">Board</h2>
          {isLoading && <div className="text-neutral-400">Loading…</div>}
          {board && board.total === 0 && (
            <div className="text-neutral-400 text-sm">No issues yet.</div>
          )}
          {board &&
            Object.entries(board.by_state).map(([state, issues]) => (
              <div key={state} className="mb-4">
                <div className="text-xs uppercase tracking-wide text-neutral-500 mb-1">
                  {state} ({issues.length})
                </div>
                <div className="flex flex-col gap-2">
                  {issues.map((i) => (
                    <div
                      key={i.id}
                      className="border border-neutral-700 rounded-lg p-3"
                      data-testid={`issue-${i.id}`}
                    >
                      <div className="text-sm font-medium">{i.title}</div>
                      <div className="text-xs text-neutral-400 mt-1 flex gap-2 flex-wrap">
                        {i.assignee_role && (
                          <span className="bg-neutral-800 rounded px-1.5">
                            {i.assignee_role}
                          </span>
                        )}
                        {i.risk && (
                          <span className="bg-red-900/60 text-red-200 rounded px-1.5">
                            risk:{i.risk}
                          </span>
                        )}
                        <span>priority {i.priority}</span>
                        {i.github_ref && <span>{i.github_ref}</span>}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ))}
        </div>

        {/* Roster */}
        <div>
          <h2 className="text-sm font-medium text-neutral-300 mb-2">Roster</h2>
          <div className="flex flex-col gap-2">
            {agents?.map((a) => (
              <div
                key={a.role}
                className="border border-neutral-700 rounded-lg p-3"
                data-testid={`agent-${a.role}`}
              >
                <div className="text-sm font-medium">{a.display_name}</div>
                <div className="text-xs text-neutral-400 mt-1">
                  {a.role} · {a.agent_kind || a.actor_kind}
                  {a.acp_server && ` (${a.acp_server})`}
                  {a.created_by_role && ` · by ${a.created_by_role}`}
                </div>
              </div>
            ))}
            {agents && agents.length === 0 && (
              <div className="text-neutral-400 text-sm">No members yet.</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export default TeamDetailScreen;
