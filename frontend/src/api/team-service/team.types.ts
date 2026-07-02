// Types for the AI Team API (/api/v1/teams). Mirrors the backend cockpit
// responses in openhands/app_server/team/router.py.

export interface TeamSummary {
  id: string;
  name: string;
  github_identity: string | null;
  repos: string[];
  lead_conversation_id: string | null;
  enabled: boolean;
  created_at: string;
  // health fields (added by the list endpoint)
  agents: number;
  lead_configured: boolean;
  open_issues: number;
  needs_attention: number;
}

export interface TeamIssue {
  id: string;
  team_id: string;
  origin: string;
  github_ref: string | null;
  repo: string | null;
  title: string;
  body: string | null;
  author_role: string;
  assignee_role: string | null;
  state: string;
  priority: string;
  risk: string | null;
  round_count: number;
  created_at: string;
  updated_at: string;
  stuck_since: string | null;
}

export interface TeamDashboard {
  team_id: string;
  filter: string;
  total: number;
  by_state: Record<string, TeamIssue[]>;
}

export interface TeamAgent {
  role: string;
  display_name: string;
  actor_kind: string;
  agent_kind: string | null;
  acp_server: string | null;
  llm_model: string | null;
  enabled: boolean;
  created_by_role: string | null;
}

export interface TeamEvent {
  id: number;
  actor_role: string;
  kind: string;
  from_state: string | null;
  to_state: string | null;
  conversation_id: string | null;
  detail: Record<string, unknown>;
  created_at: string;
}

export interface TeamIssueDetail {
  issue: TeamIssue;
  comments: {
    id: string;
    author_role: string;
    provenance: string;
    conversation_id: string | null;
    body: string;
    created_at: string;
  }[];
  events: TeamEvent[];
}

export interface CreateTeamRequest {
  name: string;
  github_identity?: string;
  repos?: string[];
  needs?: string;
}

export interface CreateTeamResponse {
  team: TeamSummary;
  lead: { role: string; github_identity: string | null };
  lead_conversation_id: string | null;
  formation_issue_id: string | null;
}
