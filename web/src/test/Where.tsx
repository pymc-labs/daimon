import { useLocation } from "react-router"

export const Where = () => <div data-testid="where">{useLocation().pathname + useLocation().search}</div>
