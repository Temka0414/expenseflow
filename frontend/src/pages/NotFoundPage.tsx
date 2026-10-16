import { Link } from 'react-router';
import { EmptyState } from '../components/ui';

export default function NotFoundPage() {
  return (
    <EmptyState
      title="Page not found"
      action={
        <Link to="/" className="btn btn-primary btn-sm">
          Go to the dashboard
        </Link>
      }
    >
      CloudFront serves index.html for every path, so the app itself decides what exists.
    </EmptyState>
  );
}
