import { useMutation, useQueryClient } from "@tanstack/react-query";
import TeamService from "#/api/team-service/team-service.api";
import { CreateTeamRequest } from "#/api/team-service/team.types";
import { TEAMS_QUERY_KEY } from "#/hooks/query/use-teams";

/** Create an AI team (and spawn its lead). Invalidates the teams list. */
export function useCreateTeam() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: CreateTeamRequest) => TeamService.createTeam(body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: [TEAMS_QUERY_KEY] });
    },
  });
}
