/* eslint-disable i18next/no-literal-string */
// Internal operator UI (AI Teams). Literal strings are acceptable here, matching
// the precedent in admin-dashboard.tsx / plan-preview.tsx; not user-i18n scope.
import React from "react";
import { Link } from "react-router";
import { useTeams } from "#/hooks/query/use-teams";
import { useCreateTeam } from "#/hooks/mutation/use-create-team";

/**
 * AI Teams list — the main-page entry into the multi-team system (design §15).
 * Lists all teams with a "needs you" badge and a create form. Read-mostly:
 * steering a team happens in its lead conversation, not here.
 */
function TeamsScreen() {
  const { data: teams, isLoading, error } = useTeams();
  const createTeam = useCreateTeam();

  const [name, setName] = React.useState("");
  const [identity, setIdentity] = React.useState("");
  const [repos, setRepos] = React.useState("");
  const [needs, setNeeds] = React.useState("");

  const onCreate = (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    createTeam.mutate(
      {
        name: name.trim(),
        github_identity: identity.trim() || undefined,
        repos: repos
          .split(",")
          .map((r) => r.trim())
          .filter(Boolean),
        needs: needs.trim() || undefined,
      },
      {
        onSuccess: () => {
          setName("");
          setRepos("");
          setNeeds("");
        },
      },
    );
  };

  return (
    <div
      data-testid="teams-screen"
      className="px-6 py-8 h-full overflow-y-auto custom-scrollbar-always max-w-[900px] mx-auto w-full"
    >
      <h1 className="text-xl font-semibold mb-1">AI Teams</h1>
      <p className="text-sm text-neutral-400 mb-6">
        Supervised multi-agent engineering teams. Create a team to spawn its
        lead, then steer it from the team&apos;s lead conversation.
      </p>

      {/* Create form */}
      <form
        onSubmit={onCreate}
        data-testid="create-team-form"
        className="border border-neutral-700 rounded-xl p-4 mb-8 flex flex-col gap-3"
      >
        <div className="font-medium">New team</div>
        <input
          className="bg-neutral-800 rounded-md px-3 py-2 text-sm"
          placeholder="Team name (e.g. Web)"
          value={name}
          onChange={(e) => setName(e.target.value)}
          data-testid="team-name-input"
        />
        <input
          className="bg-neutral-800 rounded-md px-3 py-2 text-sm"
          placeholder="GitHub identity (login the lead posts as)"
          value={identity}
          onChange={(e) => setIdentity(e.target.value)}
        />
        <input
          className="bg-neutral-800 rounded-md px-3 py-2 text-sm"
          placeholder="Watched repos, comma-separated (owner/repo, ...)"
          value={repos}
          onChange={(e) => setRepos(e.target.value)}
        />
        <textarea
          className="bg-neutral-800 rounded-md px-3 py-2 text-sm"
          placeholder="What should this team cover? (the lead forms its roster from this)"
          value={needs}
          onChange={(e) => setNeeds(e.target.value)}
          rows={2}
        />
        <button
          type="submit"
          disabled={createTeam.isPending || !name.trim()}
          className="self-start bg-primary text-black rounded-md px-4 py-2 text-sm font-medium disabled:opacity-50"
          data-testid="create-team-submit"
        >
          {createTeam.isPending ? "Creating…" : "Create team"}
        </button>
        {createTeam.isError && (
          <div className="text-red-400 text-sm">
            Failed to create team. Check the GitHub identity and try again.
          </div>
        )}
      </form>

      {/* Teams list */}
      {isLoading && <div className="text-neutral-400">Loading teams…</div>}
      {error && <div className="text-red-400">Failed to load teams.</div>}
      {teams && teams.length === 0 && (
        <div className="text-neutral-400">No teams yet. Create one above.</div>
      )}
      <div className="flex flex-col gap-2">
        {teams?.map((t) => (
          <Link
            key={t.id}
            to={`/teams/${t.id}`}
            data-testid={`team-card-${t.id}`}
            className="border border-neutral-700 rounded-xl p-4 hover:border-neutral-500 flex items-center gap-3"
          >
            <div className="grow">
              <div className="font-medium">{t.name}</div>
              <div className="text-xs text-neutral-400">
                {t.github_identity || "no identity"} · {t.open_issues} open ·
                lead {t.lead_configured ? "✓" : "—"}
                {t.repos.length > 0 && ` · ${t.repos.join(", ")}`}
              </div>
            </div>
            {t.needs_attention > 0 && (
              <span className="bg-amber-300 text-black rounded-full px-2 py-0.5 text-xs">
                {t.needs_attention} need you
              </span>
            )}
          </Link>
        ))}
      </div>
    </div>
  );
}

export default TeamsScreen;
