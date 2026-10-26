import { Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import Approvals from "./pages/Approvals";
import Audit from "./pages/Audit";
import Deployments from "./pages/Deployments";
import Evaluation from "./pages/Evaluation";
import FaultLab from "./pages/FaultLab";
import IncidentDetail from "./pages/IncidentDetail";
import Incidents from "./pages/Incidents";
import Knowledge from "./pages/Knowledge";
import Overview from "./pages/Overview";
import PipelineDetail from "./pages/PipelineDetail";
import Pipelines from "./pages/Pipelines";
import Policies from "./pages/Policies";
import System from "./pages/System";
import Tests from "./pages/Tests";

export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Overview />} />
        <Route path="/pipelines" element={<Pipelines />} />
        <Route path="/pipelines/:id" element={<PipelineDetail />} />
        <Route path="/tests" element={<Tests />} />
        <Route path="/deployments" element={<Deployments />} />
        <Route path="/incidents" element={<Incidents />} />
        <Route path="/incidents/:id" element={<IncidentDetail />} />
        <Route path="/approvals" element={<Approvals />} />
        <Route path="/fault-lab" element={<FaultLab />} />
        <Route path="/knowledge" element={<Knowledge />} />
        <Route path="/evaluation" element={<Evaluation />} />
        <Route path="/policies" element={<Policies />} />
        <Route path="/audit" element={<Audit />} />
        <Route path="/system" element={<System />} />
        <Route path="*" element={<Overview />} />
      </Routes>
    </Layout>
  );
}
