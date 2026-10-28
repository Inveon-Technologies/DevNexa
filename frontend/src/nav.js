import {
  BookOpenCheck, FlaskConical, FlaskRound, GitBranch, Gauge, Layers, ListChecks, Rocket, ScrollText, ShieldCheck,
  Siren, UserCheck,
} from "lucide-react";

/**
 * Sidebar navigation. `main` is what everyday monitoring needs and is always visible;
 * `more` holds the deeper research and governance pages behind a collapsible section.
 * To add a page: add its <Route> in App.jsx and one entry here.
 * `badge` names a count from /api/overview kpis shown next to the label.
 */
export const NAV = {
  main: [
    { to: "/", label: "Dashboard", icon: Gauge },
    { to: "/pipelines", label: "Pipelines", icon: GitBranch },
    { to: "/deployments", label: "Deployments", icon: Rocket },
    { to: "/incidents", label: "Incidents", icon: Siren, badge: "open_incidents" },
    { to: "/approvals", label: "Approvals", icon: UserCheck, badge: "pending_approvals" },
  ],
  more: [
    { to: "/fault-lab", label: "Fault lab", icon: FlaskConical },
    { to: "/tests", label: "Test intelligence", icon: ListChecks },
    { to: "/knowledge", label: "Incident memory", icon: BookOpenCheck },
    { to: "/evaluation", label: "Evaluation", icon: FlaskRound },
    { to: "/policies", label: "Risk policy", icon: ShieldCheck },
    { to: "/audit", label: "Audit log", icon: ScrollText },
    { to: "/system", label: "Architecture", icon: Layers },
  ],
};
