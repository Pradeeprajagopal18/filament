// The frontend build tool (Vite 8 / rolldown) needs Node 20.19+ or 22.12+. On older versions it
// fails with "Cannot find native binding", which doesn't say what's wrong.
const [major, minor] = process.versions.node.split('.').map(Number)
const ok = major > 22 || (major === 22 && minor >= 12) || (major === 20 && minor >= 19)
if (!ok) {
  console.error(`Node ${process.versions.node} is too old for the frontend. Install Node 22 ` +
    '(nvm install 22, or brew install node@22), then: rm -rf frontend/node_modules && make setup')
  process.exit(1)
}
