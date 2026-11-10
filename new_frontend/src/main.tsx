import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";

class DashboardBoundary extends React.Component<React.PropsWithChildren, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (this.state.failed) {
      return <main role="alert">
        <h1>The dashboard could not display this view.</h1>
        <p>Reload and check recent runs before starting another repair.</p>
        <button onClick={() => window.location.reload()}>Reload dashboard</button>
      </main>;
    }
    return this.props.children;
  }
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <DashboardBoundary><App /></DashboardBoundary>
  </React.StrictMode>,
);
