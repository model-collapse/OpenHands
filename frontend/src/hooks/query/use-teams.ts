import { useQuery } from "@tanstack/react-query";
import TeamService from "#/api/team-service/team-service.api";

export const TEAMS_QUERY_KEY = "teams";

/** List all AI teams (with per-team health). */
export function useTeams() {
  return useQuery({
    queryKey: [TEAMS_QUERY_KEY],
    queryFn: () => TeamService.listTeams(),
    staleTime: 1000 * 15,
  });
}

/** One team's board (issues grouped by state). */
export function useTeamBoard(teamId: string | undefined, filter = "all") {
  return useQuery({
    queryKey: [TEAMS_QUERY_KEY, teamId, "board", filter],
    enabled: !!teamId,
    queryFn: () => TeamService.getDashboard(teamId as string, filter),
    staleTime: 1000 * 10,
  });
}

/** One team's roster. */
export function useTeamAgents(teamId: string | undefined) {
  return useQuery({
    queryKey: [TEAMS_QUERY_KEY, teamId, "agents"],
    enabled: !!teamId,
    queryFn: () => TeamService.getAgents(teamId as string),
    staleTime: 1000 * 30,
  });
}

/** The id of a team's grand-leader<->lead conversation (for the "open chat" link). */
export function useTeamLeadConversation(teamId: string | undefined) {
  return useQuery({
    queryKey: [TEAMS_QUERY_KEY, teamId, "lead-conversation"],
    enabled: !!teamId,
    queryFn: () => TeamService.getLeadConversationId(teamId as string),
    staleTime: 1000 * 60,
  });
}
