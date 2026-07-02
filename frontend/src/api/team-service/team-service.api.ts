import { openHands } from "../open-hands-axios";
import {
  CreateTeamRequest,
  CreateTeamResponse,
  TeamAgent,
  TeamDashboard,
  TeamIssueDetail,
  TeamSummary,
} from "./team.types";

const BASE = "/api/v1/teams";

/**
 * Data-access layer for the AI Team API. Never call these directly from
 * components — wrap in a TanStack Query hook (see hooks/query, hooks/mutation).
 */
class TeamService {
  static async listTeams(): Promise<TeamSummary[]> {
    const { data } = await openHands.get<{ items: TeamSummary[] }>(BASE);
    return data.items;
  }

  static async createTeam(
    body: CreateTeamRequest,
  ): Promise<CreateTeamResponse> {
    const { data } = await openHands.post<CreateTeamResponse>(BASE, body);
    return data;
  }

  static async getDashboard(
    teamId: string,
    filter = "attention",
  ): Promise<TeamDashboard> {
    const { data } = await openHands.get<TeamDashboard>(
      `${BASE}/${teamId}/dashboard`,
      { params: { filter } },
    );
    return data;
  }

  static async getAgents(teamId: string): Promise<TeamAgent[]> {
    const { data } = await openHands.get<{ items: TeamAgent[] }>(
      `${BASE}/${teamId}/agents`,
    );
    return data.items;
  }

  static async getIssue(
    teamId: string,
    issueId: string,
  ): Promise<TeamIssueDetail> {
    const { data } = await openHands.get<TeamIssueDetail>(
      `${BASE}/${teamId}/issues/${issueId}`,
    );
    return data;
  }

  static async getLeadConversationId(teamId: string): Promise<string | null> {
    const { data } = await openHands.get<{ conversation_id: string | null }>(
      `${BASE}/${teamId}/lead-conversation`,
    );
    return data.conversation_id;
  }
}

export default TeamService;
