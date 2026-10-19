import { createContext, useContext } from 'react'

// The auth context, useAuth and the user types. The root component is in
// AppRoot.tsx (see the note there); everything here keeps its '../App' import
// path, which the whole app and its test mocks use.

// Mirrors server/roles.py STORABLE_ROLES. 'cashier' was missing although the
// server staffs cashier accounts, so the type hid a role every login could carry.
// The five HIS roles (migration 48) exist only while their feature is on; see
// ROLE_FEATURE in src/lib/roles.ts.
export type UserRole =
  | 'doctor' | 'nurse' | 'admin' | 'patient' | 'pharmacist' | 'cashier'
  | 'receptionist' | 'ward_clerk' | 'lab_technician' | 'pathologist' | 'phlebotomist'
  | null

/** The facility an account works at (server UserOut.facility, multisite-5a).
 *  A label only: what the account may see is decided on the server. `name`
 *  is the display name; facility 1 may carry none (the site name stands in). */
export interface UserFacility {
  id: number
  code: string | null
  name: string | null
}

export interface User {
  id: string
  name: string
  role: UserRole
  department?: string
  registrationNumber?: string
  /** 'pin' = signed in with a PIN on a shared, enrolled ward tablet. */
  authMethod?: 'password' | 'pin'
  /** multisite-6: shown beside the role only while more than one facility is
   *  on record (ClinicianShell reads /api/auth/me for a fresh one). */
  facility?: UserFacility | null
}

interface AuthContextType {
  user: User | null
  login: (user: User) => void
  logout: () => void
}

export const AuthContext = createContext<AuthContextType>({
  user: null,
  login: () => {},
  logout: () => {},
})

export const useAuth = () => useContext(AuthContext)
