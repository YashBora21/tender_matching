/** A thrown value as something a person can read. */
export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
