import { Navigate } from 'react-router-dom';
import { WorkspaceShell } from '@/components/workspace/WorkspaceShell';
import { useWorkspaceStore } from '@/stores/workspaceStore';

export function WorkspacePage() {
  const { groups, selectedWorkspaceId } = useWorkspaceStore();
  const group = groups.find(item => item.workspace_id === selectedWorkspaceId);
  if (group) return <Navigate to={`/workspace/groups/${group.id}`} replace />;
  return <WorkspaceShell />;
}
