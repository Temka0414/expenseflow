import { lazy } from 'react';
import { Route, Routes } from 'react-router';
import { useSession } from '../platform/context';
import { AppShell } from './AppShell';
import { RequireRole } from './RequireRole';

// Route-level code splitting: each page is its own chunk, fetched on first visit.
const DashboardPage = lazy(() => import('../pages/DashboardPage'));
const ExpensesPage = lazy(() => import('../pages/ExpensesPage'));
const NewExpensePage = lazy(() => import('../pages/NewExpensePage'));
const ExpenseDetailPage = lazy(() => import('../pages/ExpenseDetailPage'));
const ApprovalsPage = lazy(() => import('../pages/ApprovalsPage'));
const ReportsPage = lazy(() => import('../pages/ReportsPage'));
const LearnPage = lazy(() => import('../pages/LearnPage'));
const NotFoundPage = lazy(() => import('../pages/NotFoundPage'));

export function AppRoutes() {
  const { roles } = useSession();
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<DashboardPage />} />
        <Route path="expenses" element={<ExpensesPage />} />
        <Route path="expenses/new" element={<NewExpensePage />} />
        <Route path="expenses/:expenseId" element={<ExpenseDetailPage />} />
        <Route
          path="approvals"
          element={
            <RequireRole allowed={roles.canApprove} who="managers and admins">
              <ApprovalsPage />
            </RequireRole>
          }
        />
        <Route
          path="reports"
          element={
            <RequireRole allowed={roles.isAdmin} who="admins">
              <ReportsPage />
            </RequireRole>
          }
        />
        <Route path="learn" element={<LearnPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
