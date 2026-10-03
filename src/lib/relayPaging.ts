/** Page size of a Relay thread read. A page of exactly this many means there
 *  may be older messages; a shorter one means the start of the thread is
 *  reached. Shared by api/client (the request) and RelayPanel (the "Show
 *  earlier messages" decision), so the two cannot disagree. */
export const RELAY_PAGE_SIZE = 50
