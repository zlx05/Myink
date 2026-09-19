import type { Project } from '../types'

export function isProjectDraft(project: Project): boolean {
  return project.creation_status === 'draft' || project.creation_status === 'setup_confirmed'
}

export function projectHref(project: Project): string {
  return isProjectDraft(project)
    ? `/projects/new?draft=${encodeURIComponent(project.id)}`
    : `/projects/${project.id}`
}
