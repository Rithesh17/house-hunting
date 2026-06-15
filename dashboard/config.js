/* Public Supabase connection for the read-only dashboard.
 * The anon key is SAFE to expose: row-level security allows SELECT only, so the
 * worst a visitor can do is read the same listings the dashboard shows. All
 * writes happen locally via the service_role key (never shipped to the browser). */
window.DASHBOARD_CONFIG = {
  SUPABASE_URL: "https://tlgxbeglgcfftjfmdicf.supabase.co",
  SUPABASE_ANON_KEY: "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRsZ3hiZWdsZ2NmZnRqZm1kaWNmIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODE1NDQ1MjMsImV4cCI6MjA5NzEyMDUyM30.5IAInHbccMeG9oRGi0vOwBUNFUg96rdxhbYdfIW1puc",
};
